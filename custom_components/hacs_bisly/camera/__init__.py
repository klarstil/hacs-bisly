"""Camera platform for hacs_bisly."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import math
import time
from typing import TYPE_CHECKING, Any

import aiohttp

from custom_components.hacs_bisly.const import (
    CAMERA_IMAGE_CACHE_WINDOW,
    CAMERA_IMAGE_URL,
    DOMAIN,
    LOGGER,
    MANUFACTURER,
    MODEL,
    WEBRTC_TURN_CREDENTIAL,
    WEBRTC_TURN_SERVERS,
    WEBRTC_TURN_USERNAME,
)
from homeassistant.components.camera import Camera, CameraEntityDescription, CameraEntityFeature
from homeassistant.components.camera.webrtc import WebRTCAnswer, WebRTCCandidate, WebRTCError, WebRTCSendMessage
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo

try:
    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCIceCandidate,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )
except ImportError:
    MediaStreamTrack = None  # type: ignore[assignment,misc]
    RTCConfiguration = None  # type: ignore[assignment,misc]
    RTCIceCandidate = None  # type: ignore[assignment,misc]
    RTCIceServer = None  # type: ignore[assignment,misc]
    RTCPeerConnection = None  # type: ignore[assignment,misc]
    RTCSessionDescription = None  # type: ignore[assignment,misc]

if TYPE_CHECKING:
    from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator
    from custom_components.hacs_bisly.data import BislyConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

PARALLEL_UPDATES = 0
_ACTIVE_SESSIONS: dict[str, _BislyWebRTCSession] = {}

_HANDLER_INSTALLED = False


def _install_exception_handler() -> None:
    global _HANDLER_INSTALLED
    if _HANDLER_INSTALLED:
        return
    _HANDLER_INSTALLED = True
    loop = asyncio.get_running_loop()
    orig = loop.get_exception_handler()

    def handler(loop, context):
        msg = str(context.get("message", ""))
        if "Transaction.__retry" in msg or "socket.send" in msg:
            return
        exc = context.get("exception")
        if exc is not None and type(exc).__qualname__ in ("AttributeError", "InvalidStateError", "OSError"):
            return
        if orig is not None:
            orig(loop, context)
        else:
            loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


def _parse_ice(s: str, mid: str, idx: int) -> RTCIceCandidate:
    if s.startswith("candidate:"):
        p = s[10:].split(" ")
        if len(p) >= 8:
            return RTCIceCandidate(
                component=int(p[1]),
                foundation=p[0],
                ip=p[4],
                port=int(p[5]),
                priority=int(p[3]),
                protocol=p[2],
                type=p[7],
                sdpMid=mid,
                sdpMLineIndex=idx,
            )
    return RTCIceCandidate(
        component=idx,
        foundation="",
        ip="",
        port=0,
        priority=0,
        protocol="udp",
        type="",
        sdpMid=mid,
        sdpMLineIndex=idx,
    )


class _BislyWebRTCSession:
    def __init__(self, sid: str, cid: str, cli: Any, send: WebRTCSendMessage) -> None:
        self.session_id = sid
        self.camera_uuid = cid
        self.client = cli
        self.send_message = send
        self._bid: str | None = None
        self._bpc: RTCPeerConnection | None = None
        self._hpc: RTCPeerConnection | None = None
        self._closed = False
        self._pice: list[RTCIceCandidate] = []
        self._err_sent = False

    # ----------------------------------------------------------------

    async def start(self, offer_sdp: str) -> None:
        if RTCPeerConnection is None:
            self.send_message(WebRTCError("aiortc_missing", "aiortc not installed"))
            return

        srv = [
            RTCIceServer(urls=[u], username=WEBRTC_TURN_USERNAME, credential=WEBRTC_TURN_CREDENTIAL)
            for u in WEBRTC_TURN_SERVERS
        ]
        cfg = RTCConfiguration(iceServers=srv)

        # -------- HA frontend PC: answer immediately ----------
        self._hpc = RTCPeerConnection(configuration=cfg)

        @self._hpc.on("connectionstatechange")
        async def ha_conn() -> None:
            if self._closed or not self._hpc or self._hpc.connectionState != "failed":
                return
            if not self._err_sent:
                self._err_sent = True
                self.send_message(WebRTCError("connection_lost", "HA peer failed"))
            await self.close()

        @self._hpc.on("icecandidate")
        async def ha_ice(c: RTCIceCandidate) -> None:
            if c.candidate and c.sdpMid is not None:
                self.send_message(
                    WebRTCCandidate(
                        {
                            "candidate": c.candidate,
                            "sdpMid": c.sdpMid,
                            "sdpMLineIndex": c.sdpMLineIndex or 0,
                        }
                    )
                )

        try:
            offer = RTCSessionDescription(sdp=offer_sdp, type="offer")
            await self._hpc.setRemoteDescription(offer)
        except Exception as exc:
            if not self._err_sent:
                self._err_sent = True
                self.send_message(WebRTCError("setup_failed", str(exc)))
            await self.close()
            return

        # Grab sender ref + flip to sendonly so the browser VC decoder
        # doesn't try to decode phantom frames.
        ha_sender = None
        for t in self._hpc.getTransceivers():
            if t.kind == "video":
                ha_sender = t.sender
                if t.direction in ("recvonly", "sendrecv"):
                    t.direction = "sendonly"
                break

        try:
            ans = await self._hpc.createAnswer()
            await self._hpc.setLocalDescription(ans)
            self.send_message(WebRTCAnswer(ans.sdp))
        except Exception as exc:
            if not self._err_sent:
                self._err_sent = True
                self.send_message(WebRTCError("setup_failed", str(exc)))
            await self.close()
            return

        # -------- Bisly videoserver PC ----------
        self._bpc = RTCPeerConnection(configuration=cfg)

        @self._bpc.on("track")
        def b_track(track: MediaStreamTrack) -> None:
            if track.kind == "video" and self._hpc and not self._closed and ha_sender:
                ha_sender.replaceTrack(track)
                LOGGER.info("Bisly track bridged (session=%s)", self.session_id)

        @self._bpc.on("icecandidate")
        async def b_ice(c: RTCIceCandidate) -> None:
            if c.candidate and self._bid:
                await self.client.send_videoserver_ice(
                    connection_id=self._bid,
                    candidate=c.candidate,
                    sdp_mid=c.sdpMid or "",
                    sdp_mline_index=c.sdpMLineIndex or 0,
                )

        @self._bpc.on("connectionstatechange")
        async def b_conn() -> None:
            if self._closed or not self._bpc or self._bpc.connectionState != "failed":
                return
            if not self._err_sent:
                self._err_sent = True
                self.send_message(WebRTCError("bisly_failed", "Bisly peer failed"))
            await self.close()

        resp = await self.client.open_videoserver(self.camera_uuid)
        if not resp:
            self.send_message(WebRTCError("open_failed", "No response"))
            await self.close()
            return

        self._bid = str(resp.get("id") or "")
        b64 = resp.get("param", "")
        if not self._bid or not b64:
            self.send_message(WebRTCError("open_failed", "Empty offer"))
            await self.close()
            return

        off = json.loads(base64.b64decode(b64).decode())
        bsdp = off.get("sdp", "")
        if not bsdp:
            self.send_message(WebRTCError("open_failed", "No SDP"))
            await self.close()
            return

        try:
            await self._bpc.setRemoteDescription(RTCSessionDescription(sdp=bsdp, type="offer"))
        except Exception as exc:
            if not self._err_sent:
                self._err_sent = True
                self.send_message(WebRTCError("setup_failed", str(exc)))
            await self.close()
            return

        for ice in self._pice:
            with contextlib.suppress(Exception):
                await self._bpc.addIceCandidate(ice)
        self._pice.clear()

        try:
            bans = await self._bpc.createAnswer()
            await self._bpc.setLocalDescription(bans)
        except Exception as exc:
            if not self._err_sent:
                self._err_sent = True
                self.send_message(WebRTCError("setup_failed", str(exc)))
            await self.close()
            return

        await self.client.answer_videoserver(
            self._bid,
            sdp_base64=base64.b64encode(json.dumps({"sdp": bans.sdp, "type": bans.type}).encode()).decode(),
        )

        LOGGER.info("WebRTC session established (session=%s)", self.session_id)

    # ----------------------------------------------------------------

    async def handle_candidate(self, c: Any) -> None:
        if self._closed or not self._hpc or not self._hpc.remoteDescription:
            return
        cs = getattr(c, "candidate", "") or ""
        mid = getattr(c, "sdp_mid", "") or ""
        idx = getattr(c, "sdp_m_line_index", 0) or 0
        await self._hpc.addIceCandidate(_parse_ice(cs, mid, idx))

    async def handle_bisly_ice(self, j: dict[str, Any]) -> None:
        if self._closed:
            return
        ice = _parse_ice(j.get("candidate", ""), j.get("sdpMid", ""), j.get("sdpMLineIndex", 0))
        if self._bpc and self._bpc.remoteDescription:
            await self._bpc.addIceCandidate(ice)
        else:
            self._pice.append(ice)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        a, b = self._hpc, self._bpc
        self._hpc = self._bpc = None
        _ACTIVE_SESSIONS.pop(self.session_id, None)
        for pc in (a, b):
            if pc:
                with contextlib.suppress(BaseException):
                    await pc.close()
        if self._bid:
            with contextlib.suppress(BaseException):
                await self.client.close_videoserver(self._bid)
        LOGGER.info("WebRTC session closed (session=%s)", self.session_id)


# ================================================================


class BislyCamera(Camera):
    _attr_has_entity_name = False
    _attr_is_streaming = False
    _attr_is_recording = False
    _attr_motion_detection_enabled = False
    _attr_supported_features: CameraEntityFeature = CameraEntityFeature.STREAM

    def __init__(self, coord, desc, cdata):
        super().__init__()
        self.entity_description = desc
        self.coordinator = coord
        self._cd = cdata
        self._cid = str(cdata.get("id", ""))
        self._cuid = cdata.get("camera_uuid", self._cid)
        self._attr_name = cdata.get("label", f"Camera {self._cid}")
        self._attr_unique_id = f"{coord.config_entry.entry_id}_camera_{self._cid}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coord.config_entry.entry_id}_camera_{self._cid}")},
            name=self._attr_name,
            manufacturer=MANUFACTURER,
            model=MODEL,
            via_device=(DOMAIN, coord.config_entry.entry_id),
        )
        self._srv = coord.config_entry.runtime_data.client.server_id

    @property
    def _cli(self) -> Any:
        return self.coordinator.config_entry.runtime_data.client

    async def async_camera_image(self, width=None, height=None):
        url = CAMERA_IMAGE_URL.format(server_id=self._srv, camera_id=self._cuid)
        url += f"?cb={math.floor(time.time() / CAMERA_IMAGE_CACHE_WINDOW)}"
        try:
            async with self._cli.session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                if r.status == 200:
                    return await r.read()
        except aiohttp.ClientError, TimeoutError:
            pass
        return None

    async def async_handle_async_webrtc_offer(self, offer, sid, send):
        LOGGER.info("WebRTC offer received (session=%s)", sid)
        _install_exception_handler()
        for old in list(_ACTIVE_SESSIONS.values()):
            with contextlib.suppress(BaseException):
                await old.close()
        s = _BislyWebRTCSession(sid, self._cuid, self._cli, send)
        _ACTIVE_SESSIONS[sid] = s
        try:
            await s.start(offer)
        except Exception as exc:
            LOGGER.exception("WebRTC start failed: %s", exc)
            send(WebRTCError("webrtc_setup_failed", str(exc)))
            await s.close()

    async def async_on_webrtc_candidate(self, sid, c):
        s = _ACTIVE_SESSIONS.get(sid)
        if s:
            with contextlib.suppress(BaseException):
                await s.handle_candidate(c)

    @callback
    def close_webrtc_session(self, sid):
        s = _ACTIVE_SESSIONS.get(sid)
        if s:
            _ = asyncio.ensure_future(s.close())


async def async_setup_entry(hass, entry, aae):
    cdata = (entry.runtime_data.coordinator.data or {}).get("cameras", [])
    aae(
        [
            BislyCamera(
                entry.runtime_data.coordinator,
                CameraEntityDescription(key=f"camera_{d.get('id', '')}", translation_key="bisly_camera"),
                d,
            )
            for d in cdata
            if d.get("id")
        ]
    )
