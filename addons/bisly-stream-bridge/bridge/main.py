"""Bisly Stream Bridge entry point.

Authenticates with the Bisly cloud, discovers cameras, and exposes a small
HTTP API used by mediamtx runOnDemand to start/stop per-camera RTSP streams.

Environment variables (set by run.sh):
  BISLY_USERNAME / BISLY_PASSWORD   Bisly account credentials
  RTSP_PORT                         mediamtx RTSP port
  VIDEO_WIDTH / VIDEO_FPS           transcode target size/fps
  FFMPEG_CPU_PRESET                 ffmpeg x264 preset
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import os
from typing import Any

import aiohttp
from aiohttp import web

from .client import BislyApiClient, BislyApiClientError, attach_camera_uuids, extract_cameras
from .constants import LOGGER
from .stream import BislyStreamBridge

HTTP_PORT = 4599

LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO")

logging.basicConfig(level=getattr(logging, LOG_LEVEL.upper(), logging.INFO), format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def _unwrap(payload: Any) -> dict[str, Any]:
    """Return the ``data`` object of a Supervisor API response, if wrapped."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload
    return {}


class BridgeApp:
    """Manages authentication, camera discovery and per-camera bridges."""

    def __init__(self) -> None:
        self.username = os.environ.get("BISLY_USERNAME", "")
        self.password = os.environ.get("BISLY_PASSWORD", "")
        self.rtsp_port = int(os.environ.get("RTSP_PORT", "8554"))
        self.options: dict[str, Any] = {
            "video_width": int(os.environ.get("VIDEO_WIDTH", "1280")),
            "video_fps": int(os.environ.get("VIDEO_FPS", "15")),
            "ffmpeg_cpu_preset": os.environ.get("FFMPEG_CPU_PRESET", "veryfast"),
        }

        self.session: aiohttp.ClientSession | None = None
        self.client: BislyApiClient | None = None
        self.bridges: dict[str, BislyStreamBridge] = {}
        self.connection_lock = asyncio.Lock()
        self._ice_buffer: dict[str, list[dict[str, Any]]] = {}
        self._supervisor_token = os.environ.get("SUPERVISOR_TOKEN", "")
        self._discovery_uuid: str | None = None

    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Authenticate, discover cameras and register bridges."""
        if not self.username or not self.password:
            raise RuntimeError("BISLY_USERNAME and BISLY_PASSWORD are required")

        self.session = aiohttp.ClientSession()
        self.client = BislyApiClient(self.username, self.password, self.session)

        await self.client.authenticate()
        await self.client.connect(self._on_broadcast)

        cameras_resp = await self.client.get_cameras()
        cameras = extract_cameras(cameras_resp)
        try:
            uuid_resp = await self.client.get_camera_uuids()
        except BislyApiClientError as exc:
            LOGGER.warning("get_camera_uuids failed: %s", exc)
            uuid_resp = None
        attach_camera_uuids(cameras, extract_cameras(uuid_resp))

        rtsp_base = f"rtsp://localhost:{self.rtsp_port}"
        for camera in cameras:
            if not camera.get("camera_uuid"):
                LOGGER.warning("Camera %s has no UUID, skipping", camera.get("label") or camera.get("id"))
                continue
            bridge = BislyStreamBridge(self.client, camera, rtsp_base, self.options)
            self.bridges[bridge.slug] = bridge
            LOGGER.info("Registered camera '%s' → %s", bridge.name, bridge.rtsp_url)

        if not self.bridges:
            LOGGER.warning("No cameras with UUIDs discovered")

        await self._announce_discovery()

    async def shutdown(self) -> None:
        """Stop all bridges and close the connection."""
        await self._withdraw_discovery()
        for bridge in self.bridges.values():
            with contextlib.suppress(Exception):
                await bridge.stop()
        if self.client is not None:
            with contextlib.suppress(BislyApiClientError, Exception):
                await self.client.disconnect()
        if self.session is not None:
            await self.session.close()

    # ------------------------------------------------------------------

    async def _on_broadcast(self, message: dict[str, Any]) -> None:
        """Route incoming videoserver ICE candidates to active sessions."""
        if message.get("type") != "ice" or message.get("command") != "videoserver":
            return

        connection_id = str(message.get("id", ""))
        param = message.get("param", "")
        if not connection_id or not param:
            return

        try:
            candidate_json = json.loads(base64.b64decode(param).decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            LOGGER.debug("Failed to decode videoserver ICE candidate")
            return

        for bridge in self.bridges.values():
            session = bridge.session
            if session is not None and getattr(session, "_bid", "") == connection_id:
                LOGGER.info("Routing ICE candidate to session (connection_id=%s)", connection_id)
                asyncio.ensure_future(session.handle_bisly_ice(candidate_json))
                return

        # The candidate arrived before the videoserver open reply registered
        # the connection_id — buffer it and flush once the session exists.
        buffered = self._ice_buffer.setdefault(connection_id, [])
        if len(buffered) < 20:
            buffered.append(candidate_json)
        LOGGER.info("Buffered ICE candidate for future session (connection_id=%s)", connection_id)

    # ------------------------------------------------------------------

    async def handle_start(self, request: web.Request) -> web.Response:
        """Start the bridge for a camera (mediamtx runOnDemand)."""
        slug = request.match_info["slug"]
        bridge = self.bridges.get(slug)
        if bridge is None:
            return web.json_response({"error": f"unknown camera '{slug}'"}, status=404)

        async with self.connection_lock:
            if bridge.session is not None:
                return web.json_response({"status": "already_running", "camera": slug})

            await self._ensure_connection()
            try:
                await bridge.start()
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("Failed to start bridge for '%s'", slug)
                return web.json_response({"error": str(exc)}, status=500)

            self._flush_ice_buffer(bridge)

        return web.json_response({"status": "running", "camera": slug, "rtsp": bridge.rtsp_url})

    def _flush_ice_buffer(self, bridge: BislyStreamBridge) -> None:
        """Deliver ICE candidates that arrived before the session registered."""
        session = bridge.session
        if session is None:
            return
        connection_id = getattr(session, "_bid", "")
        if not connection_id:
            return
        buffered = self._ice_buffer.pop(connection_id, [])
        if buffered:
            LOGGER.info("Flushing %d buffered ICE candidates to session (connection_id=%s)", len(buffered), connection_id)
        for candidate_json in buffered:
            asyncio.ensure_future(session.handle_bisly_ice(candidate_json))

    async def handle_stop(self, request: web.Request) -> web.Response:
        """Stop the bridge for a camera (mediamtx runOnUnDemand)."""
        slug = request.match_info["slug"]
        bridge = self.bridges.get(slug)
        if bridge is None:
            return web.json_response({"error": f"unknown camera '{slug}'"}, status=404)

        await bridge.stop()
        return web.json_response({"status": "stopped", "camera": slug})

    async def handle_list(self, request: web.Request) -> web.Response:
        """List registered cameras and their RTSP URLs."""
        return web.json_response(
            {
                "cameras": [
                    {"slug": bridge.slug, "name": bridge.name, "rtsp": bridge.rtsp_url, "running": bridge.session is not None}
                    for bridge in self.bridges.values()
                ]
            }
        )

    async def _ensure_connection(self) -> None:
        """Re-establish the Bisly connection when the transport dropped."""
        if self.client is None or self.session is None:
            raise RuntimeError("Bridge app not started")

        if not self.client.session.closed and self.client._transport.connected:  # noqa: SLF001
            return

        await self.client.authenticate()
        await self.client.connect(self._on_broadcast)

    # ------------------------------------------------------------------
    # Supervisor discovery
    # ------------------------------------------------------------------

    async def _announce_discovery(self) -> None:
        """Register the RTSP endpoint with Home Assistant via Supervisor discovery."""
        if not self._supervisor_token or self.session is None:
            LOGGER.debug("No SUPERVISOR_TOKEN — skipping discovery (not running as an app)")
            return

        host = await self._resolve_hostname()
        if not host:
            LOGGER.warning("Could not resolve own hostname — skipping discovery")
            return

        config: dict[str, Any] = {
            "host": host,
            "port": self.rtsp_port,
            "cameras": [{"id": bridge.camera_id, "slug": bridge.slug} for bridge in self.bridges.values()],
        }
        try:
            async with self.session.post(
                f"{SUPERVISOR_URL}/discovery",
                json={"service": DISCOVERY_SERVICE, "config": config},
                headers={"Authorization": f"Bearer {self._supervisor_token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                payload = await resp.json()
        except (aiohttp.ClientError, TimeoutError, ValueError) as exc:
            LOGGER.warning("Supervisor discovery failed: %s", exc)
            return

        uuid = _unwrap(payload).get("uuid")
        if uuid:
            self._discovery_uuid = str(uuid)
            LOGGER.info("Registered with Home Assistant (uuid=%s, host=%s)", self._discovery_uuid, host)

    async def _withdraw_discovery(self) -> None:
        """Remove the discovery entry when the app stops."""
        uuid = self._discovery_uuid
        self._discovery_uuid = None
        if not uuid or not self._supervisor_token or self.session is None:
            return
        with contextlib.suppress(aiohttp.ClientError, TimeoutError, ValueError):
            async with self.session.delete(
                f"{SUPERVISOR_URL}/discovery/{uuid}",
                headers={"Authorization": f"Bearer {self._supervisor_token}"},
                timeout=aiohttp.ClientTimeout(total=10),
            ):
                pass

    async def _resolve_hostname(self) -> str:
        """Return the DNS name Home Assistant Core can use to reach this app."""
        if self._supervisor_token and self.session is not None:
            with contextlib.suppress(aiohttp.ClientError, TimeoutError, ValueError):
                async with self.session.get(
                    f"{SUPERVISOR_URL}/addons/self/info",
                    headers={"Authorization": f"Bearer {self._supervisor_token}"},
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    payload = await resp.json()
                hostname = _unwrap(payload).get("hostname")
                if hostname:
                    return str(hostname)
        return socket.gethostname().replace("_", "-")

    def build_web_app(self) -> web.Application:
        """Build the aiohttp web application with the control endpoints."""
        web_app = web.Application()
        web_app.router.add_get("/api/cameras", self.handle_list)
        web_app.router.add_post("/api/start/{slug}", self.handle_start)
        web_app.router.add_post("/api/stop/{slug}", self.handle_stop)
        return web_app


async def main() -> None:
    """Run the bridge app with its HTTP control API."""
    app = BridgeApp()
    try:
        await app.start()
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Startup failed: %s", exc)
        await app.shutdown()
        raise SystemExit(1) from exc

    runner = web.AppRunner(app.build_web_app())
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", HTTP_PORT)
    await site.start()
    LOGGER.info("Control API listening on http://127.0.0.1:%d", HTTP_PORT)

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()
        await app.shutdown()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
