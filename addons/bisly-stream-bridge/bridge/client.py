"""Bisly API client for the stream bridge.

Ported from custom_components/hacs_bisly/api/client.py — reduced to the
authentication, camera discovery and videoserver commands the bridge needs.
"""

from __future__ import annotations

import base64
import json
from typing import Any

import aiohttp

from .constants import CTRL_TYPE_CAMERA, LOGGER, SUBJECT_BROADCAST, SUBJECT_ROUTING
from .transport import BislyNATSConnectionError, BislyNATSTransport


class BislyApiClientError(Exception):
    """Base exception for API errors."""


class BislyApiClientAuthenticationError(BislyApiClientError):
    """Authentication with the Bisly cloud failed."""


class BislyApiClientCommunicationError(BislyApiClientError):
    """Communication with the Bisly cloud failed."""


class BislyApiClient:
    """High-level client for Bisly authentication and videoserver commands."""

    def __init__(self, username: str, password: str, session: aiohttp.ClientSession) -> None:
        self._username = username
        self._password = password
        self._transport = BislyNATSTransport(session)

        self._auth_hash: str = ""
        self._user_id: int = 0
        self._server_id: str = ""
        self._request_counter: int = 2
        self._handshake_config: str = ""
        self._authenticated: bool = False
        self._broadcast_subject: str = ""

    @property
    def session(self) -> aiohttp.ClientSession:
        """The aiohttp ClientSession used by this client."""
        return self._transport._session  # noqa: SLF001

    @property
    def handshake_config(self) -> str:
        """The handshake config JSON string with camera UUIDs."""
        return self._handshake_config

    @property
    def server_id(self) -> str:
        """The current server ID."""
        return self._server_id

    async def authenticate(self) -> dict[str, Any]:
        """Authenticate with the Bisly system via the cloud.auth handshake."""
        LOGGER.info("Authenticating to Bisly as %s", self._username)

        if not self._transport.connected:
            await self._transport.connect()

        handshake_msg = {
            "command": "handshake",
            "username": self._username,
            "password": self._password,
            "action": "get",
            "user_id": None,
        }

        try:
            response = await self._request(handshake_msg, timeout=30.0)
        except TimeoutError as exc:
            raise BislyApiClientCommunicationError("Authentication handshake timed out") from exc
        except BislyNATSConnectionError as exc:
            raise BislyApiClientCommunicationError(f"Authentication handshake failed: {exc}") from exc

        if response is None:
            raise BislyApiClientAuthenticationError("Authentication failed: no response from server")

        if response.get("param") == "authenticated" and response.get("auth_hash"):
            self._auth_hash = response["auth_hash"]
            self._user_id = int(response.get("user_id", 0))
            self._server_id = response.get("serverID") or ""
            self._handshake_config = response.get("config", "")
            self._authenticated = True
        else:
            raise BislyApiClientAuthenticationError(f"Authentication failed: {response}")

        LOGGER.info("Authenticated to Bisly (user_id=%d, server_id=%s)", self._user_id, self._server_id)
        return response

    async def connect(self, on_broadcast) -> None:
        """Subscribe to broadcast state updates after authentication."""
        if not self._authenticated:
            await self.authenticate()

        if not self._transport.connected:
            await self._transport.connect()

        self._transport.add_broadcast_listener(on_broadcast)

        if self._server_id:
            self._broadcast_subject = f"{SUBJECT_BROADCAST}.{self._server_id}"
            await self._transport.subscribe(self._broadcast_subject)
            LOGGER.debug("Subscribed to broadcasts: %s", self._broadcast_subject)

        self._transport.add_reconnect_callback(self._on_reconnect)

    async def _on_reconnect(self) -> None:
        """Re-establish broadcast subscription after transport reconnect."""
        if self._broadcast_subject:
            await self._transport.subscribe(self._broadcast_subject)
            LOGGER.debug("Re-subscribed to broadcasts after reconnect: %s", self._broadcast_subject)

    async def disconnect(self) -> None:
        """Disconnect from the Bisly system."""
        await self._transport.disconnect()
        self._authenticated = False
        self._broadcast_subject = ""

    async def _request(self, payload: dict[str, Any], timeout: float = 20.0) -> dict[str, Any] | None:
        """Send a request and wait for the reply."""
        req_id = self._next_id()
        payload["request_id"] = req_id

        if payload.get("command") != "handshake":
            payload["auth_hash"] = self._auth_hash
            payload["user_id"] = self._user_id
        if "serverID" not in payload:
            payload["serverID"] = self._server_id

        subject = self._subject_for(payload)
        LOGGER.debug("API request: %s → %s", payload.get("command"), subject)
        result = await self._transport.publish(subject, payload, req_id, timeout)
        LOGGER.debug(
            "API response: %s → %s...",
            payload.get("command"),
            json.dumps(result)[:200] if result else "None",
        )
        return result

    def _next_id(self) -> int:
        """Generate the next request ID."""
        self._request_counter += 1
        return self._request_counter

    @staticmethod
    def _subject_for(payload: dict[str, Any]) -> str:
        """Route a command payload to the correct NATS subject."""
        cmd = payload.get("command", "")
        sid = str(payload.get("serverID", ""))

        if cmd in SUBJECT_ROUTING:
            return SUBJECT_ROUTING[cmd]

        if cmd == "videoserver":
            return f"videoserver.{sid}"

        return f"commands.{sid}"

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    async def get_cameras(self) -> dict[str, Any] | None:
        """Fetch the list of cameras (controller_list type 14 = VIDEOPHONE)."""
        return await self._request(
            {
                "command": "controller_list",
                "action": "get",
                "type": CTRL_TYPE_CAMERA,
            }
        )

    async def get_camera_uuids(self) -> dict[str, Any] | None:
        """Fetch the camera UUID list (type 14, param "1" — as the Bisly app does)."""
        return await self._request(
            {
                "command": "controller_list",
                "action": "get",
                "type": CTRL_TYPE_CAMERA,
                "param": "1",
            }
        )

    # ------------------------------------------------------------------
    # WebRTC videoserver commands
    # ------------------------------------------------------------------

    async def open_videoserver(self, camera_id: str) -> dict[str, Any] | None:
        """Open a WebRTC video session with the Bisly videoserver."""
        return await self._request(
            {
                "command": "videoserver",
                "action": "get",
                "type": "open",
                "id": camera_id,
                "param": "",
            }
        )

    async def answer_videoserver(self, connection_id: str, sdp_base64: str) -> dict[str, Any] | None:
        """Send the local SDP answer back to the videoserver."""
        return await self._request(
            {
                "command": "videoserver",
                "action": "set",
                "type": "answer",
                "id": connection_id,
                "param": sdp_base64,
            }
        )

    async def send_videoserver_ice(
        self,
        connection_id: str,
        candidate: str,
        sdp_mid: str,
        sdp_mline_index: int,
    ) -> dict[str, Any] | None:
        """Send a local ICE candidate to the videoserver."""
        ice_payload = base64.b64encode(
            json.dumps(
                {
                    "candidate": candidate,
                    "sdpMid": sdp_mid,
                    "sdpMLineIndex": sdp_mline_index,
                }
            ).encode()
        ).decode()
        return await self._request(
            {
                "command": "videoserver",
                "action": "set",
                "type": "ice",
                "id": connection_id,
                "param": ice_payload,
            }
        )

    async def close_videoserver(self, connection_id: str) -> dict[str, Any] | None:
        """Close the videoserver connection."""
        return await self._request(
            {
                "command": "videoserver",
                "action": "set",
                "type": "close",
                "id": connection_id,
                "param": f"CLOSE,{connection_id}",
            }
        )


def extract_cameras(response: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Extract a flat camera list from a get_cameras API response."""
    if not response:
        return []

    param = response.get("param")
    if isinstance(param, list):
        return [dict(entry) for entry in param if isinstance(entry, dict)]

    if isinstance(param, dict):
        cameras: list[dict[str, Any]] = []
        for cam_id_str, cam_data in param.items():
            if isinstance(cam_data, dict):
                entry = dict(cam_data)
                entry["camera_id"] = cam_id_str
                cameras.append(entry)
        return cameras

    return []


def attach_camera_uuids(cameras: list[dict[str, Any]], uuid_list: list[dict[str, Any]] | None) -> None:
    """Attach the real camera UUIDs to the camera dicts.

    The plain camera list (type 14) returns local numeric ids; the CDN and
    videoserver need the real UUIDs, which only the type-14 param-1 query
    returns.  Matched by sip id — the only stable shared identifier.
    """
    if not cameras or not uuid_list:
        return

    for camera in cameras:
        sip = str(camera.get("sip", "")).strip()
        if not sip:
            continue
        for entry in uuid_list:
            if str(entry.get("sip", "")).strip() != sip:
                continue
            uuid = entry.get("id")
            if uuid:
                camera["camera_uuid"] = uuid
                camera["video_url"] = entry.get("video_url", camera.get("video_url", ""))
            break
