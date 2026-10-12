"""Native aiortc WebRTC support for the Bisly camera.

Terminates the Bisly WebRTC leg in-process and bridges its video track to the
browser's peer connection. This needs aiortc, which currently cannot be
installed alongside Home Assistant releases that pin a PyAV major version
aiortc does not accept (aiortc requires ``av<18``). When aiortc is missing,
``BislyCamera`` omits :class:`WebRTCNativeMixin` and streams through Home
Assistant's go2rtc/HLS pipeline via ``stream_source()`` instead.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
from typing import TYPE_CHECKING, Any

from webrtc_models import RTCIceCandidateInit

from custom_components.hacs_bisly.const import LOGGER, WEBRTC_TURN_CREDENTIAL, WEBRTC_TURN_SERVERS, WEBRTC_TURN_USERNAME
from homeassistant.components.camera.webrtc import WebRTCAnswer, WebRTCError, WebRTCSendMessage
from homeassistant.core import callback

try:
    from aiortc import (
        MediaStreamTrack,
        RTCConfiguration,
        RTCIceCandidate,
        RTCIceServer,
        RTCPeerConnection,
        RTCSessionDescription,
    )

    AIORTC_AVAILABLE = True
except ImportError:  # pragma: no cover - depends on the HA/aiortc combination
    AIORTC_AVAILABLE = False

_ACTIVE_SESSIONS: dict[str, _BislyWebRTCSession] = {}

_BACKGROUND_TASKS: set[asyncio.Task[None]] = set()

_HANDLER_INSTALLED = False


def _install_exception_handler() -> None:
    global _HANDLER_INSTALLED  # noqa: PLW0603
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
    """Bridges one Bisly videoserver WebRTC leg to the frontend's peer connection."""

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

    async def start(self, offer_sdp: str) -> None:  # noqa: C901
        srv = [
            RTCIceServer(urls=[u], username=WEBRTC_TURN_USERNAME, credential=WEBRTC_TURN_CREDENTIAL)
            for u in WEBRTC_TURN_SERVERS
        ]
        # aiortc only treats a "stun:" scheme URL as a STUN source; a "turn:" URL
        # is never used for server-reflexive gathering even against the same
        # host. Without this, only host and TURN-relay candidates are gathered,
        # and no reachable candidate exists at all when the relay allocation
        # is unusable.
        srv += [RTCIceServer(urls=[u.replace("turn:", "stun:", 1)]) for u in WEBRTC_TURN_SERVERS]
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
        except Exception as exc:  # noqa: BLE001
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
            answer = await self._hpc.createAnswer()
            await self._hpc.setLocalDescription(answer)
            self.send_message(WebRTCAnswer(answer.sdp))
        except Exception as exc:  # noqa: BLE001
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
        except Exception as exc:  # noqa: BLE001
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
            bisly_answer = await self._bpc.createAnswer()
            await self._bpc.setLocalDescription(bisly_answer)
        except Exception as exc:  # noqa: BLE001
            if self._closed:
                return
            if not self._err_sent:
                self._err_sent = True
                self.send_message(WebRTCError("setup_failed", str(exc)))
            await self.close()
            return

        if self._closed:
            return

        # Send the plain answer immediately, before ICE gathering starts —
        # matching the Bisly app's own signaling flow, which trickles
        # candidates separately rather than embedding them in the SDP.
        answer_sdp = base64.b64encode(json.dumps({"sdp": bisly_answer.sdp, "type": bisly_answer.type}).encode()).decode()
        LOGGER.info(
            "Bisly SDP answer sending (session=%s, connection_id=%s, lines=%d)",
            self.session_id,
            self._bid,
            bisly_answer.sdp.count("\n") + 1 if bisly_answer.sdp else 0,
        )
        await self.client.answer_videoserver(self._bid, sdp_base64=answer_sdp)

        LOGGER.info("WebRTC session established (session=%s)", self.session_id)

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

        # aiortc never emits icecandidate events for local candidates, so
        # extract them from the internal aioice Connection of each
        # transceiver's ICE transport and trickle them to the Bisly
        # videoserver individually — the same protocol its own app uses —
        # instead of splicing them into the SDP text.
        sent = 0
        for idx, transceiver in enumerate(self._bpc.getTransceivers()):
            ice_transport = getattr(getattr(transceiver.receiver, "transport", None), "transport", None)
            conn = getattr(ice_transport, "_connection", None)
            if conn is None:
                continue
            sdp_mid = transceiver.mid or str(idx)
            for c in getattr(conn, "_local_candidates", []):
                LOGGER.debug(
                    "Bisly PC local candidate: %s %s:%s (session=%s)",
                    c.type,
                    c.host,
                    c.port,
                    self.session_id,
                )
                if self._closed:
                    return
                await self.client.send_videoserver_ice(
                    self._bid,
                    candidate=f"candidate:{c.to_sdp()}",
                    sdp_mid=sdp_mid,
                    sdp_mline_index=idx,
                )
                sent += 1
        if not sent:
            LOGGER.debug("Bisly PC no local candidates extracted (session=%s)", self.session_id)

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


class WebRTCNativeMixin:
    """Native aiortc WebRTC offer handling, mixed into ``BislyCamera`` when aiortc is available.

    The offer/candidate handlers exist only when aiortc is importable. Without
    them ``BislyCamera`` inherits ``Camera``'s default implementation, so Home
    Assistant falls back to its go2rtc/HLS pipeline via ``stream_source()``.
    """

    if TYPE_CHECKING:
        coordinator: Any
        _cli: Any

        def _resolve_cuid(self) -> str: ...

    if AIORTC_AVAILABLE:

        async def async_handle_async_webrtc_offer(
            self, offer_sdp: str, session_id: str, send_message: WebRTCSendMessage
        ) -> None:
            """Handle a WebRTC offer from the frontend by bridging it to the videoserver."""
            LOGGER.info("WebRTC offer received (session=%s)", session_id)
            _install_exception_handler()
            for old in list(_ACTIVE_SESSIONS.values()):
                with contextlib.suppress(BaseException):
                    await old.close()
            session = _BislyWebRTCSession(
                session_id,
                self._resolve_cuid(),
                self._cli,
                send_message,
                intercom=getattr(self.coordinator.config_entry.runtime_data, "intercom", None),
            )
            _ACTIVE_SESSIONS[session_id] = session
            try:
                await session.start(offer_sdp)
            except Exception as exc:  # noqa: BLE001
                LOGGER.exception("WebRTC start failed: %s", exc)
                send_message(WebRTCError("webrtc_setup_failed", str(exc)))
                await session.close()

        async def async_on_webrtc_candidate(self, session_id: str, candidate: RTCIceCandidateInit) -> None:
            """Forward a frontend ICE candidate to the active session."""
            session = _ACTIVE_SESSIONS.get(session_id)
            if session:
                with contextlib.suppress(BaseException):
                    await session.handle_candidate(candidate)

        @callback
        def close_webrtc_session(self, session_id: str) -> None:
            """Close the WebRTC session for a frontend session id."""
            session = _ACTIVE_SESSIONS.get(session_id)
            if session:
                task = asyncio.ensure_future(session.close())
                _BACKGROUND_TASKS.add(task)
                task.add_done_callback(_BACKGROUND_TASKS.discard)


__all__ = ["AIORTC_AVAILABLE", "WebRTCNativeMixin"]
