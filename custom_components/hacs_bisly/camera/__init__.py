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
from homeassistant.components.camera.webrtc import WebRTCAnswer, WebRTCError, WebRTCSendMessage
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
    def __init__(self, sid: str, cid: str, cli: Any, send: WebRTCSendMessage, intercom: Any = None) -> None:
        self.session_id = sid
        self.camera_uuid = cid
        self.client = cli
        self.send_message = send
        self.intercom = intercom
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
            if not self._hpc:
                return
            LOGGER.info("HA peer connection state: %s (session=%s)", self._hpc.connectionState, self.session_id)
            if self._closed or self._hpc.connectionState != "failed":
                return
            if not self._err_sent:
                self._err_sent = True
                self.send_message(WebRTCError("connection_lost", "HA peer failed"))
            await self.close()

        try:
            offer = RTCSessionDescription(sdp=offer_sdp, type="offer")
            await self._hpc.setRemoteDescription(offer)
        except Exception as exc:
            if self._closed:
                return
            if not self._err_sent:
                self._err_sent = True
                self.send_message(WebRTCError("setup_failed", str(exc)))
            await self.close()
            return

        if self._closed:
            # Session was superseded by a newer offer during setup
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
            if self._closed:
                return
            if not self._err_sent:
                self._err_sent = True
                self.send_message(WebRTCError("setup_failed", str(exc)))
            await self.close()
            return

        if self._closed:
            return

        # If an intercom call is ringing/active, bridge visitor audio into
        # this session's HA-facing peer connection (the frontend opened an
        # audio transceiver in recvonly).
        if self.intercom is not None:
            with contextlib.suppress(Exception):
                await self.intercom.attach_audio(self)

        # -------- Bisly videoserver PC ----------
        self._bpc = RTCPeerConnection(configuration=cfg)

        @self._bpc.on("track")
        def b_track(track: MediaStreamTrack) -> None:
            if track.kind == "video" and self._hpc and not self._closed and ha_sender:
                ha_sender.replaceTrack(track)
                LOGGER.info("Bisly track bridged (session=%s)", self.session_id)

        @self._bpc.on("connectionstatechange")
        async def b_conn() -> None:
            if not self._bpc:
                return
            LOGGER.info(
                "Bisly peer connection state: %s (session=%s, ice=%s)",
                self._bpc.connectionState,
                self.session_id,
                self._bpc.iceConnectionState if hasattr(self._bpc, "iceConnectionState") else "?",
            )
            if self._closed or self._bpc.connectionState != "failed":
                return
            if not self._err_sent:
                self._err_sent = True
                self.send_message(WebRTCError("bisly_failed", "Bisly peer failed"))
            await self.close()

        resp = await self.client.open_videoserver(self.camera_uuid)
        if self._closed:
            return
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

        try:
            raw_offer = base64.b64decode(b64, validate=True)
        except ValueError:
            # The videoserver replies with plain-text status messages when the
            # stream is not ready yet (e.g. "Camera stream is not ready yet,
            # please retry") — not base64, so report it instead of crashing.
            self.send_message(WebRTCError("open_failed", str(b64)[:120]))
            await self.close()
            return

        try:
            off = json.loads(raw_offer.decode("utf-8"))
        except UnicodeDecodeError:
            # The videoserver can encode offers as binary strings
            # (JavaScript atob semantics, Latin-1), not UTF-8.
            off = json.loads(raw_offer.decode("latin-1"))
        except json.JSONDecodeError:
            self.send_message(WebRTCError("open_failed", "Invalid offer payload"))
            await self.close()
            return

        bsdp = off.get("sdp", "") if isinstance(off, dict) else ""
        LOGGER.info(
            "Bisly SDP offer received (session=%s, connection_id=%s, lines=%d)",
            self.session_id,
            self._bid,
            bsdp.count("\n") + 1 if bsdp else 0,
        )
        if not bsdp:
            self.send_message(WebRTCError("open_failed", "No SDP"))
            await self.close()
            return

        try:
            await self._bpc.setRemoteDescription(RTCSessionDescription(sdp=bsdp, type="offer"))
        except Exception as exc:
            if self._closed:
                return
            if not self._err_sent:
                self._err_sent = True
                self.send_message(WebRTCError("setup_failed", str(exc)))
            await self.close()
            return

        if self._closed:
            return

        for ice in self._pice:
            with contextlib.suppress(Exception):
                await self._bpc.addIceCandidate(ice)
        self._pice.clear()

        try:
            bans = await self._bpc.createAnswer()
            await self._bpc.setLocalDescription(bans)
        except Exception as exc:
            if self._closed:
                return
            if not self._err_sent:
                self._err_sent = True
                self.send_message(WebRTCError("setup_failed", str(exc)))
            await self.close()
            return

        if self._closed:
            return

        # Wait for ICE gathering to complete so candidates are available.
        if self._bpc.iceGatheringState != "complete":
            gather_event = asyncio.Event()

            @self._bpc.on("icegatheringstatechange")
            def _on_gathering_done() -> None:
                if self._bpc and self._bpc.iceGatheringState == "complete":
                    gather_event.set()

            await gather_event.wait()

        if self._closed:
            return

        # aiortc never emits icecandidate events for local candidates, and
        # localDescription.sdp is not updated after ICE gathering.  Extract
        # candidates from the internal aioice Connection so the Bisly server
        # knows where to send STUN bindings.
        candidate_lines: list[str] = []
        ice_transports = getattr(self._bpc, "_RTCPeerConnection__iceTransports", None)
        if ice_transports:
            for transport in ice_transports:
                conn = getattr(transport, "_connection", None)
                if conn is None:
                    continue
                for c in getattr(conn, "_local_candidates", []):
                    cstr = (
                        f"candidate:{c.foundation} {c.component} {c.transport}"
                        f" {c.priority} {c.host} {c.port} typ {c.type}"
                    )
                    candidate_lines.append(f"a={cstr}")
                    LOGGER.debug(
                        "Bisly PC local candidate: %s %s:%s (session=%s)",
                        c.type,
                        c.host,
                        c.port,
                        self.session_id,
                    )
        if not candidate_lines:
            LOGGER.debug(
                "Bisly PC no local candidates extracted (session=%s, transports=%s)",
                self.session_id,
                bool(ice_transports),
            )

        sdp_with_candidates = bans.sdp
        if candidate_lines:
            sdp_with_candidates += "\r\n" + "\r\n".join(candidate_lines)

        answer_sdp = base64.b64encode(json.dumps({"sdp": sdp_with_candidates, "type": bans.type}).encode()).decode()
        LOGGER.info(
            "Bisly SDP answer sending (session=%s, connection_id=%s, lines=%d, candidates=%d)",
            self.session_id,
            self._bid,
            sdp_with_candidates.count("\n") + 1 if sdp_with_candidates else 0,
            len(candidate_lines),
        )
        await self.client.answer_videoserver(self._bid, sdp_base64=answer_sdp)

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
        if self.intercom is not None:
            with contextlib.suppress(Exception):
                await self.intercom.detach_audio(self)
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

    def _resolve_cuid(self) -> str:
        """Resolve the camera UUID from the coordinator data.

        The coordinator refreshes the camera list; use the latest UUID,
        falling back to the value captured at entity creation.
        """
        for cam in (self.coordinator.data or {}).get("cameras", []):
            if isinstance(cam, dict) and str(cam.get("id", "")) == self._cid:
                return str(cam.get("camera_uuid") or cam.get("id") or self._cid)
        return self._cuid

    async def async_camera_image(self, width=None, height=None):
        cuid = self._resolve_cuid()
        url = CAMERA_IMAGE_URL.format(server_id=self._srv, camera_id=cuid)
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
        s = _BislyWebRTCSession(
            sid,
            self._resolve_cuid(),
            self._cli,
            send,
            intercom=getattr(self.coordinator.config_entry.runtime_data, "intercom", None),
        )
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
