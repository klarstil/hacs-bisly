"""
Intercom (fono) manager for hacs_bisly.

Handles the Bisly intercom call lifecycle: incoming-call broadcasts
(type 2/3/7/8), doorbell notifications (type 10), the fono protocol
(GET_CALL/GET_ICE/GET_DOORS/GET_CAMERA, ANSWER/HANGUP/ICE_CANDIDATE),
and the WebRTC audio peer connection that bridges visitor audio into an
active HA camera stream.

State is exposed to entities via coordinator.data["intercom"].

Architecture:
    Broadcast → Coordinator → BislyIntercomManager.handle_broadcast()
    Camera session → BislyIntercomManager.attach_audio()/detach_audio()
"""

from __future__ import annotations

import base64
import contextlib
import json
import time
from typing import TYPE_CHECKING, Any

from custom_components.hacs_bisly.api import BislyApiClientError
from custom_components.hacs_bisly.const import (
    BROADCAST_CALL_END,
    BROADCAST_CALL_ENDED,
    BROADCAST_CALL_ENDED_ELSEWHERE,
    BROADCAST_DOOR_NOTIFICATION,
    BROADCAST_DOORBELL,
    BROADCAST_INTERCOM_CALL,
    FONO_ANSWER,
    FONO_HANGUP,
    FONO_ICE_CANDIDATE,
    FONO_NO_CALL,
    FONO_RING_PREFIX,
    LOGGER,
    SUBJECT_BROADCAST,
    WEBRTC_TURN_CREDENTIAL,
    WEBRTC_TURN_SERVERS,
    WEBRTC_TURN_USERNAME,
)

try:
    import aiortc
except ImportError:
    aiortc = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from custom_components.hacs_bisly.data import BislyConfigEntry
    from homeassistant.core import HomeAssistant

# How long the doorbell sensor stays on after a doorbell broadcast (seconds)
DOORBELL_ON_SECONDS = 5.0

# Fono call states
STATE_IDLE = "idle"
STATE_RINGING = "ringing"
STATE_ACTIVE = "active"


def _parse_ice(s: str, mid: str, idx: int) -> Any:
    """Parse an ICE candidate string into an aiortc RTCIceCandidate."""
    if aiortc is None:
        return None
    if s.startswith("candidate:"):
        p = s[10:].split(" ")
        if len(p) >= 8:
            return aiortc.RTCIceCandidate(
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
    return aiortc.RTCIceCandidate(
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


class BislyIntercomManager:
    """
    Manages the intercom call lifecycle and the fono audio peer connection.

    Receives broadcasts from the coordinator, drives the fono state machine,
    and bridges visitor audio into the active HA camera WebRTC session.
    """

    def __init__(self, hass: HomeAssistant, entry: BislyConfigEntry) -> None:
        """Initialize the intercom manager."""
        self._hass = hass
        self._entry = entry

        self.state: str = STATE_IDLE
        self.call_id: str = ""
        self.camera_id: str = ""
        self.door_ids: list[str] = []
        self.doorbell_at: float = 0.0

        self._ring_offer: dict[str, Any] | None = None
        self._ice_buffer: list[dict[str, Any]] = []
        self._audio_pc: Any = None
        self._bridged_session: Any = None
        self._sip_subject: str = ""
        self._doorbell_reset_handle: Any = None

    # ------------------------------------------------------------------
    # State helpers
    # ------------------------------------------------------------------

    @property
    def is_active(self) -> bool:
        """Whether an intercom call is currently ringing or active."""
        return self.state in (STATE_RINGING, STATE_ACTIVE)

    def _intercom_data(self) -> dict[str, Any]:
        """Build the coordinator intercom state dict."""
        return {
            "state": self.state,
            "call_id": self.call_id,
            "camera_id": self.camera_id,
            "doors": list(self.door_ids),
            "doorbell_at": self.doorbell_at,
        }

    async def _push_state(self) -> None:
        """Push the current intercom state into the coordinator data."""
        coordinator = self._entry.runtime_data.coordinator
        data = dict(coordinator.data or {})
        data["intercom"] = self._intercom_data()
        coordinator.async_set_updated_data(data)

    # ------------------------------------------------------------------
    # Broadcast handling (called by the coordinator)
    # ------------------------------------------------------------------

    async def handle_broadcast(self, message: dict[str, Any]) -> None:
        """Handle an intercom-related broadcast message."""
        btype = str(message.get("type", ""))
        client = self._entry.runtime_data.client

        try:
            if btype == BROADCAST_INTERCOM_CALL:
                await self._handle_incoming_call(message, client)
            elif btype in (BROADCAST_CALL_ENDED, BROADCAST_CALL_ENDED_ELSEWHERE, BROADCAST_CALL_END):
                await self._handle_call_ended()
            elif btype == BROADCAST_DOORBELL:
                await self._handle_doorbell(message, client)
            elif btype == BROADCAST_DOOR_NOTIFICATION:
                LOGGER.debug("Intercom: door notification broadcast (ignored)")
            elif message.get("command") == "fono":
                await self._handle_fono_message(message)
        except BislyApiClientError as exc:
            LOGGER.debug("Intercom broadcast handling failed: %s", exc)

    async def _handle_incoming_call(self, message: dict[str, Any], client: Any) -> None:
        """Process a type-2 broadcast: incoming intercom call."""
        with contextlib.suppress(BislyApiClientError):
            await client.ack_broadcast(message)

        # Subscribe to the per-sip intercom subject if present (app behaviour)
        sip_server = str(message.get("sip_server", ""))
        sip = str(message.get("sip", ""))
        if sip_server and sip and not self._sip_subject:
            self._sip_subject = f"{SUBJECT_BROADCAST}.{client.server_id}.{sip_server}.{sip}"
            with contextlib.suppress(BislyApiClientError):
                await client._transport.subscribe(self._sip_subject)  # noqa: SLF001
                LOGGER.debug("Intercom: subscribed to %s", self._sip_subject)

        if self.state != STATE_IDLE:
            LOGGER.debug("Intercom: incoming call while state=%s — ignored", self.state)
            return

        # Fetch current call state — GET_CALL returns RING + offer
        resp = await client.fono_get_call()
        if not resp:
            return
        param = resp.get("param", "")
        if param == FONO_NO_CALL:
            LOGGER.debug("Intercom: GET_CALL returned NO_CALL")
            return
        if not isinstance(param, str) or not param.startswith(FONO_RING_PREFIX):
            LOGGER.debug("Intercom: unexpected GET_CALL param: %s", param)
            return

        parts = param.split(",")
        if len(parts) < 3:
            LOGGER.debug("Intercom: malformed RING param: %s", param)
            return

        self.call_id = parts[1]
        try:
            offer_json = json.loads(base64.b64decode(parts[2]).decode())
        except json.JSONDecodeError, UnicodeDecodeError, ValueError:
            LOGGER.debug("Intercom: failed to decode RING offer")
            return
        self._ring_offer = offer_json if isinstance(offer_json, dict) else None
        self.state = STATE_RINGING

        # Pre-buffered ICE candidates (app fetches them before answering)
        with contextlib.suppress(BislyApiClientError):
            await self._fetch_buffered_ice(client)

        # Doors linked to this call (for open-door buttons)
        with contextlib.suppress(BislyApiClientError):
            doors_resp = await client.fono_get_doors()
            doors_param = (doors_resp or {}).get("param")
            if isinstance(doors_param, list):
                self.door_ids = [str(d) for d in doors_param]

        # Camera linked to this call
        with contextlib.suppress(BislyApiClientError):
            camera_resp = await client.fono_get_camera()
            camera_param = (camera_resp or {}).get("param")
            if isinstance(camera_param, str) and camera_param != FONO_NO_CALL:
                self.camera_id = camera_param

        await self._push_state()
        LOGGER.info(
            "Intercom: incoming call ringing (call_id=%s, camera=%s, doors=%s)",
            self.call_id,
            self.camera_id,
            self.door_ids,
        )

    async def _fetch_buffered_ice(self, client: Any) -> None:
        """Fetch pre-buffered ICE candidates via GET_ICE."""
        resp = await client.fono_get_ice()
        param = (resp or {}).get("param")
        if not isinstance(param, list):
            return
        for entry in param:
            if not isinstance(entry, str):
                continue
            parts = entry.split(",")
            if len(parts) < 3 or not parts[2]:
                continue
            try:
                candidate = json.loads(base64.b64decode(parts[2]).decode())
            except json.JSONDecodeError, UnicodeDecodeError, ValueError:
                continue
            if isinstance(candidate, dict):
                self._ice_buffer.append(candidate)

    async def _handle_call_ended(self) -> None:
        """Process call-ended broadcasts (3/7/8)."""
        if self.state == STATE_IDLE:
            return
        LOGGER.info("Intercom: call ended (call_id=%s)", self.call_id)
        await self.teardown()

    async def _handle_doorbell(self, message: dict[str, Any], client: Any) -> None:
        """Process a doorbell broadcast (type 10)."""
        with contextlib.suppress(BislyApiClientError):
            await client.ack_broadcast(message)

        self.doorbell_at = time.monotonic()
        await self._push_state()

        if self._doorbell_reset_handle is not None:
            self._doorbell_reset_handle.cancel()
        self._doorbell_reset_handle = self._hass.loop.call_later(DOORBELL_ON_SECONDS, self._reset_doorbell)
        LOGGER.debug("Intercom: doorbell pressed")

    def _reset_doorbell(self) -> None:
        """Reset the doorbell sensor after the on-window elapses."""
        self.doorbell_at = 0.0
        self._doorbell_reset_handle = None
        self._hass.async_create_task(self._push_state())

    async def _handle_fono_message(self, message: dict[str, Any]) -> None:
        """Handle inbound fono messages (ICE candidates, hangup)."""
        param = message.get("param", "")
        if not isinstance(param, str):
            return

        if param.startswith(FONO_ICE_CANDIDATE):
            parts = param.split(",")
            if len(parts) < 3:
                return
            try:
                candidate = json.loads(base64.b64decode(parts[2]).decode())
            except json.JSONDecodeError, UnicodeDecodeError, ValueError:
                return
            if isinstance(candidate, dict):
                await self._add_remote_candidate(candidate)

        elif param.startswith(FONO_HANGUP):
            LOGGER.info("Intercom: remote hangup (call_id=%s)", self.call_id)
            await self.teardown()

    async def _add_remote_candidate(self, candidate: dict[str, Any]) -> None:
        """Add a remote ICE candidate to the audio PC (or buffer it)."""
        if self._audio_pc is None or self._audio_pc.remoteDescription is None:
            self._ice_buffer.append(candidate)
            return
        ice = _parse_ice(
            candidate.get("candidate", ""),
            candidate.get("sdpMid", ""),
            candidate.get("sdpMLineIndex", 0),
        )
        if ice is not None:
            with contextlib.suppress(Exception):
                await self._audio_pc.addIceCandidate(ice)

    # ------------------------------------------------------------------
    # Audio bridging (called by the camera session)
    # ------------------------------------------------------------------

    async def attach_audio(self, session: Any) -> None:
        """
        Answer the call and bridge visitor audio into a camera session.

        Creates the aiortc audio PC, sets the remote offer from RING, flushes
        buffered ICE candidates, creates the answer, and sends ANSWER plus
        trickled ICE candidates. The incoming audio track is bridged into the
        camera session's HA-facing peer connection.

        Args:
            session: The active _BislyWebRTCSession serving the camera stream.
        """
        if not self.is_active or self._ring_offer is None:
            return
        if self._bridged_session is not None:
            LOGGER.debug("Intercom: audio already bridged — ignoring")
            return

        if aiortc is None:
            LOGGER.warning("Intercom: aiortc not installed — cannot answer call audio")
            return

        client = self._entry.runtime_data.client

        srv = [
            aiortc.RTCIceServer(urls=[u], username=WEBRTC_TURN_USERNAME, credential=WEBRTC_TURN_CREDENTIAL)
            for u in WEBRTC_TURN_SERVERS
        ]
        cfg = aiortc.RTCConfiguration(iceServers=srv)
        self._audio_pc = aiortc.RTCPeerConnection(configuration=cfg)

        @self._audio_pc.on("track")
        def _on_track(track: Any) -> None:
            if track.kind == "audio" and self.state != STATE_IDLE:
                self._hass.async_create_task(self._bridge_track_to_session(track))

        try:
            offer = self._ring_offer
            await self._audio_pc.setRemoteDescription(
                aiortc.RTCSessionDescription(sdp=offer.get("sdp", ""), type="offer")
            )

            # Flush pre-buffered candidates (app: after have-remote-offer)
            for candidate in list(self._ice_buffer):
                ice = _parse_ice(
                    candidate.get("candidate", ""),
                    candidate.get("sdpMid", ""),
                    candidate.get("sdpMLineIndex", 0),
                )
                if ice is not None:
                    with contextlib.suppress(Exception):
                        await self._audio_pc.addIceCandidate(ice)
            self._ice_buffer.clear()

            answer = await self._audio_pc.createAnswer()
            await self._audio_pc.setLocalDescription(answer)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Intercom: audio answer failed: %s", exc)
            await self._close_audio_pc()
            return

        if self.state == STATE_IDLE:
            # Call ended while we were setting up
            await self._close_audio_pc()
            return

        # Extract local candidates (aiortc does not emit icecandidate events)
        candidate_lines: list[str] = []
        ice_transports = getattr(self._audio_pc, "_RTCPeerConnection__iceTransports", None)
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

        sdp_with_candidates = answer.sdp
        if candidate_lines:
            sdp_with_candidates += "\r\n" + "\r\n".join(candidate_lines)

        answer_b64 = base64.b64encode(json.dumps({"sdp": sdp_with_candidates, "type": answer.type}).encode()).decode()

        with contextlib.suppress(BislyApiClientError):
            await client.fono_send(FONO_ANSWER, self.call_id, param=answer_b64)

        # Trickle the local candidates as ICE_CANDIDATE fono messages
        for line in candidate_lines:
            candidate_json = {
                "candidate": line[2:],
                "sdpMid": "0",
                "sdpMLineIndex": 0,
            }
            candidate_b64 = base64.b64encode(json.dumps(candidate_json).encode()).decode()
            with contextlib.suppress(BislyApiClientError):
                await client.fono_send(
                    FONO_ICE_CANDIDATE,
                    self.call_id,
                    param=f"{FONO_ICE_CANDIDATE},{self.call_id},{candidate_b64}",
                )

        self._bridged_session = session
        self.state = STATE_ACTIVE
        await self._push_state()
        LOGGER.info("Intercom: call answered, audio bridged (call_id=%s)", self.call_id)

    async def _bridge_track_to_session(self, track: Any) -> None:
        """Bridge the visitor audio track into the camera session's HA PC."""
        session = self._bridged_session
        if session is None:
            return
        hpc = getattr(session, "_hpc", None)
        if hpc is None:
            return

        # The HA frontend opened an audio transceiver (recvonly). Find it and
        # flip it to sendonly so the browser decodes our bridged track.
        sender = None
        for t in hpc.getTransceivers():
            if t.kind == "audio":
                sender = t.sender
                if t.direction in ("recvonly", "sendrecv"):
                    t.direction = "sendonly"
                break

        if sender is None:
            LOGGER.debug("Intercom: no audio transceiver in HA PC — cannot bridge audio")
            return

        with contextlib.suppress(Exception):
            sender.replaceTrack(track)
        LOGGER.info("Intercom: visitor audio bridged into camera session")

    async def detach_audio(self, session: Any) -> None:
        """Detach bridged audio when a camera session closes."""
        if self._bridged_session is session:
            self._bridged_session = None
            # Keep the call answered — only the video session ended.
            LOGGER.debug("Intercom: audio detached from camera session")

    # ------------------------------------------------------------------
    # User actions
    # ------------------------------------------------------------------

    async def hangup(self) -> None:
        """Hang up the active call (user-initiated or remote)."""
        if self.state == STATE_IDLE:
            return

        client = self._entry.runtime_data.client
        with contextlib.suppress(BislyApiClientError):
            await client.fono_send(FONO_HANGUP, self.call_id, param=self.call_id)

        await self.teardown()

    async def open_door(self, action_id: str) -> None:
        """Execute a door-open action (from fono GET_DOORS)."""
        client = self._entry.runtime_data.client
        await client.exec_action(action_id)
        LOGGER.info("Intercom: door open action executed (action_id=%s)", action_id)

    async def teardown(self) -> None:
        """Reset the intercom state and close the audio PC."""
        await self._close_audio_pc()
        self._bridged_session = None
        self.state = STATE_IDLE
        self.call_id = ""
        self.camera_id = ""
        self.door_ids = []
        self._ring_offer = None
        self._ice_buffer = []
        await self._push_state()

    async def _close_audio_pc(self) -> None:
        """Close the audio peer connection if present."""
        pc, self._audio_pc = self._audio_pc, None
        if pc is not None:
            with contextlib.suppress(BaseException):
                await pc.close()
