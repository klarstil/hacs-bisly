"""WebRTC session that bridges a Bisly camera track into an ffmpeg RTSP pipe.

Ported from custom_components/hacs_bisly/camera/__init__.py::_BislyWebRTCSession,
but instead of bridging into the Home Assistant frontend peer connection, the
video track is decoded and piped as raw frames into an ffmpeg process that
pushes H.264 to the local mediamtx RTSP server.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import subprocess
import threading
from typing import Any

from aiortc import (
    MediaStreamTrack,
    RTCConfiguration,
    RTCIceCandidate,
    RTCIceServer,
    RTCPeerConnection,
    RTCSessionDescription,
)

from .constants import LOGGER, WEBRTC_TURN_CREDENTIAL, WEBRTC_TURN_SERVERS, WEBRTC_TURN_USERNAME


class StreamBridgeError(Exception):
    """Error while setting up or running a stream bridge."""


def _parse_ice(s: str, mid: str, idx: int) -> RTCIceCandidate:
    """Parse an ICE candidate string into an aiortc RTCIceCandidate."""
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


class FfmpegRtspSink:
    """Pipes raw BGR frames into ffmpeg, which pushes RTSP to mediamtx."""

    def __init__(self, rtsp_url: str, width: int, height: int, fps: int, preset: str) -> None:
        self._rtsp_url = rtsp_url
        self._width = width
        self._height = height
        self._fps = fps
        self._preset = preset
        self._proc: subprocess.Popen[bytes] | None = None
        self._started = False
        self._write_errors = 0

    def start(self) -> None:
        """Start the ffmpeg subprocess."""
        if self._started:
            return

        cmd = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{self._width}x{self._height}",
            "-r",
            str(self._fps),
            "-i",
            "pipe:0",
            "-an",
            "-c:v",
            "libx264",
            "-preset",
            self._preset,
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-f",
            "rtsp",
            self._rtsp_url,
        ]
        LOGGER.info("Starting ffmpeg: %s", " ".join(cmd))
        self._proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
        self._started = True
        threading.Thread(target=self._drain_stderr, daemon=True).start()

    def _drain_stderr(self) -> None:
        """Forward ffmpeg stderr lines to the log."""
        if self._proc is None or self._proc.stderr is None:
            return
        for raw in self._proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line:
                LOGGER.warning("ffmpeg: %s", line)

    def write(self, frame_bytes: bytes) -> None:
        """Write a raw BGR frame to the ffmpeg stdin pipe."""
        if self._proc is None or self._proc.stdin is None or self._proc.poll() is not None:
            self._write_errors += 1
            if self._write_errors <= 5:
                LOGGER.warning("ffmpeg not running, dropping frame (errors=%d)", self._write_errors)
            return
        self._write_errors = 0
        try:
            self._proc.stdin.write(frame_bytes)
        except BrokenPipeError:
            LOGGER.warning("ffmpeg stdin closed (broken pipe)")
            self._started = False

    async def stop(self) -> None:
        """Stop the ffmpeg subprocess."""
        if self._proc is not None and self._proc.stdin is not None:
            try:
                self._proc.stdin.close()
            except Exception:  # noqa: BLE001
                pass
        try:
            if self._proc is not None:
                self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            if self._proc is not None:
                self._proc.terminate()
                self._proc.wait(timeout=5)
        self._started = False
        self._proc = None


class BislyStreamBridgeSession:
    """Terminates the Bisly WebRTC offer/answer and pipes video to ffmpeg."""

    def __init__(
        self,
        client: Any,
        camera_uuid: str,
        rtsp_url: str,
        width: int,
        height: int,
        fps: int,
        preset: str,
    ) -> None:
        self.client = client
        self.camera_uuid = camera_uuid
        self.rtsp_url = rtsp_url
        self.width = width
        self.height = height
        self.fps = fps
        self.preset = preset

        self._pc: RTCPeerConnection | None = None
        self._bid: str | None = None
        self._pice: list[RTCIceCandidate] = []
        self._closed = False
        self._sink: FfmpegRtspSink | None = None
        self._video_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Run the offer/answer exchange and start the ffmpeg pipe on first track."""
        srv = [
            RTCIceServer(urls=[u], username=WEBRTC_TURN_USERNAME, credential=WEBRTC_TURN_CREDENTIAL)
            for u in WEBRTC_TURN_SERVERS
        ]
        cfg = RTCConfiguration(iceServers=srv)

        self._pc = RTCPeerConnection(configuration=cfg)

        @self._pc.on("connectionstatechange")
        async def _on_conn_state() -> None:
            if not self._pc:
                return
            LOGGER.info(
                "Bisly PC connection state: %s (camera=%s)",
                self._pc.connectionState,
                self.camera_uuid,
            )
            if self._closed or self._pc.connectionState != "failed":
                return
            await self.close()

        @self._pc.on("iceconnectionstatechange")
        async def _on_ice_state() -> None:
            if self._pc and hasattr(self._pc, "iceConnectionState"):
                LOGGER.info(
                    "Bisly PC ICE state: %s (camera=%s)",
                    self._pc.iceConnectionState,
                    self.camera_uuid,
                )

        @self._pc.on("track")
        def on_track(track: MediaStreamTrack) -> None:
            if track.kind != "video" or self._closed:
                return
            LOGGER.info("Bisly video track received (session=%s)", self._bid)
            if self._video_task is None or self._video_task.done():
                self._video_task = asyncio.ensure_future(self._consume_track(track))

        resp = await self.client.open_videoserver(self.camera_uuid)
        if self._closed or not resp:
            raise StreamBridgeError("No response from videoserver open")

        self._bid = str(resp.get("id") or "")
        b64 = resp.get("param", "")
        if not self._bid or not b64:
            raise StreamBridgeError("Empty videoserver offer")

        off = json.loads(base64.b64decode(b64).decode())
        bsdp = off.get("sdp", "")
        LOGGER.info(
            "Bisly SDP offer received (camera=%s, connection_id=%s, lines=%d)",
            self.camera_uuid,
            self._bid,
            bsdp.count("\n") + 1 if bsdp else 0,
        )
        LOGGER.info("Bisly SDP offer:\n%s", bsdp)
        if not bsdp:
            raise StreamBridgeError("No SDP in videoserver offer")

        await self._pc.setRemoteDescription(RTCSessionDescription(sdp=bsdp, type="offer"))

        for ice in self._pice:
            await self._pc.addIceCandidate(ice)
        self._pice.clear()

        answer = await self._pc.createAnswer()
        await self._pc.setLocalDescription(answer)

        if self._pc.iceGatheringState != "complete":
            gather_event = asyncio.Event()

            @self._pc.on("icegatheringstatechange")
            def _on_gathering_done() -> None:
                if self._pc and self._pc.iceGatheringState == "complete":
                    gather_event.set()

            await gather_event.wait()

        candidate_lines = self._extract_local_candidates()

        sdp_with_candidates = answer.sdp
        if candidate_lines:
            sdp_with_candidates += "\r\n" + "\r\n".join(candidate_lines)

        answer_sdp = base64.b64encode(json.dumps({"sdp": sdp_with_candidates, "type": answer.type}).encode()).decode()
        LOGGER.info(
            "Bisly SDP answer sending (camera=%s, connection_id=%s, candidates=%d)",
            self.camera_uuid,
            self._bid,
            len(candidate_lines),
        )
        await self.client.answer_videoserver(self._bid, sdp_base64=answer_sdp)

        LOGGER.info("WebRTC session established (camera=%s)", self.camera_uuid)

    def _extract_local_candidates(self) -> list[str]:
        """Extract local ICE candidates from aiortc internals.

        aiortc does not emit icecandidate events for local candidates, and
        localDescription.sdp is not updated after ICE gathering. The same
        approach as the Home Assistant integration: read the candidates from
        the internal aioice Connection objects.
        """
        candidate_lines: list[str] = []
        if self._pc is None:
            return candidate_lines

        ice_transports = getattr(self._pc, "_RTCPeerConnection__iceTransports", None)
        if not ice_transports:
            return candidate_lines

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
                LOGGER.debug("Local candidate: %s %s:%s", c.type, c.host, c.port)
        return candidate_lines

    # ------------------------------------------------------------------

    async def _consume_track(self, track: MediaStreamTrack) -> None:
        """Decode frames from the Bisly track and pipe them to ffmpeg."""
        width = self.width
        height = self.height
        fps = self.fps

        stop_event = asyncio.Event()

        async def _watchdog() -> None:
            while not stop_event.is_set() and not self._closed:
                with contextlib.suppress(asyncio.TimeoutError):
                    async with asyncio.timeout(10):
                        await stop_event.wait()
                if stop_event.is_set() or self._closed:
                    return
                pc = self._pc
                if pc is not None:
                    LOGGER.info(
                        "Status while waiting for first frame: ice=%s conn=%s (camera=%s)",
                        getattr(pc, "iceConnectionState", "?"),
                        pc.connectionState,
                        self.camera_uuid,
                    )
                    self._log_ice_pairs(pc)

        watchdog_task = asyncio.ensure_future(_watchdog())

        # Prefer the negotiated frame dimensions when known.
        try:
            LOGGER.info("Waiting for first frame (camera=%s, timeout=60s)", self.camera_uuid)
            async with asyncio.timeout(60):
                frame = await track.recv()
        except TimeoutError:
            LOGGER.error("No video frame received within 60s (camera=%s)", self.camera_uuid)
            await self.close()
            return
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Failed to receive first frame: %s", exc)
            return
        finally:
            stop_event.set()
            watchdog_task.cancel()
            with contextlib.suppress(Exception):
                await watchdog_task

        frame_width = getattr(frame, "width", None) or width
        frame_height = getattr(frame, "height", None) or height
        if frame_width and frame_height:
            width, height = int(frame_width), int(frame_height)

        LOGGER.info("First frame received (camera=%s, %dx%d)", self.camera_uuid, width, height)

        sink = FfmpegRtspSink(self.rtsp_url, width, height, fps, self.preset)
        self._sink = sink
        sink.start()

        try:
            while not self._closed:
                self._write_frame(sink, frame)
                frame = await track.recv()
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            LOGGER.info("Video track ended: %s", exc)
        finally:
            await sink.stop()

    def _write_frame(self, sink: FfmpegRtspSink, frame: Any) -> None:
        """Convert an aiortc VideoFrame to BGR bytes and write to ffmpeg."""
        try:
            array = frame.to_ndarray(format="bgr24")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Frame conversion failed: %s", exc)
            return
        sink.write(array.tobytes())

    @staticmethod
    def _log_ice_pairs(pc: RTCPeerConnection) -> None:
        """Log candidate-pair states from aioice internals."""
        ice_transports = getattr(pc, "_RTCPeerConnection__iceTransports", None)
        if not ice_transports:
            return
        for transport in ice_transports:
            conn = getattr(transport, "_connection", None)
            if conn is None:
                continue
            pairs = getattr(conn, "_check_list", [])
            for pair in pairs:
                LOGGER.info(
                    "ICE pair: %s state=%s nominated=%s",
                    pair,
                    getattr(pair, "state", "?"),
                    getattr(pair, "nominated", "?"),
                )

    # ------------------------------------------------------------------

    async def handle_bisly_ice(self, j: dict[str, Any]) -> None:
        """Add a remote ICE candidate received via NATS broadcast."""
        if self._closed:
            return
        LOGGER.info(
            "Bisly ICE candidate received (connection_id=%s, candidate=%.60s...)",
            self._bid,
            j.get("candidate", ""),
        )
        ice = _parse_ice(j.get("candidate", ""), j.get("sdpMid", ""), j.get("sdpMLineIndex", 0))
        if self._pc and self._pc.remoteDescription:
            await self._pc.addIceCandidate(ice)
        else:
            self._pice.append(ice)

    async def close(self) -> None:
        """Close the peer connection and stop ffmpeg."""
        if self._closed:
            return
        self._closed = True

        if self._video_task is not None:
            self._video_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._video_task

        pc = self._pc
        self._pc = None
        if pc:
            with contextlib.suppress(Exception):
                await pc.close()
        if self._bid:
            with contextlib.suppress(Exception):
                await self.client.close_videoserver(self._bid)
        LOGGER.info("WebRTC session closed (camera=%s)", self.camera_uuid)


class BislyStreamBridge:
    """Manages one camera's WebRTC session and its RTSP output."""

    def __init__(self, client: Any, camera: dict[str, Any], rtsp_base: str, options: dict[str, Any]) -> None:
        self.client = client
        self.camera = camera
        self.name = camera.get("label") or f"camera_{camera.get('id', 'unknown')}"
        self.slug = self._slugify(self.name)
        self.rtsp_url = f"{rtsp_base}/{self.slug}"
        self.options = options
        self.session: BislyStreamBridgeSession | None = None
        self.camera_uuid = camera.get("camera_uuid") or str(camera.get("id", ""))

    @staticmethod
    def _slugify(name: str) -> str:
        """Convert a camera label into an RTSP-safe ASCII path segment.

        mediamtx accepts only ASCII alphanumerics plus underscore, dot,
        tilde, minus and slash in path names, so umlauts are transliterated
        and everything else is replaced with an underscore.
        """
        for char, replacement in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
            name = name.replace(char, replacement)
            name = name.replace(char.upper(), replacement.capitalize())
        cleaned = "".join(
            c if c.isascii() and (c.isalnum() or c in ("-", "_")) else "_" for c in name.lower()
        )
        return cleaned.strip("_") or "camera"

    async def start(self) -> None:
        """Start the WebRTC session for this camera."""
        self.session = BislyStreamBridgeSession(
            self.client,
            self.camera_uuid,
            self.rtsp_url,
            int(self.options.get("video_width", 1280)),
            int(self.options.get("video_width", 1280)),
            int(self.options.get("video_fps", 15)),
            str(self.options.get("ffmpeg_cpu_preset", "veryfast")),
        )
        await self.session.start()

    async def stop(self) -> None:
        """Stop the WebRTC session for this camera."""
        if self.session is not None:
            await self.session.close()
            self.session = None
