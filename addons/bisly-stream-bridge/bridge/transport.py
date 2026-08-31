"""NATS WebSocket transport for the Bisly stream bridge.

Ported from custom_components/hacs_bisly/api/nats_transport.py without
Home Assistant dependencies.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
import json
import random
import time
from typing import Any

import aiohttp
from aiohttp import WSMsgType

from .constants import BISLY_NATS_PASS, BISLY_NATS_USER, BISLY_WS_URL, LOGGER, NATS_CONNECT_TEMPLATE

_INBOX_CHARS = "abcdefghijklmnopqrstuvwxyz123456789"
_INBOX_LENGTH = 8

_MAX_RECONNECT_ATTEMPTS = 99999
_RECONNECT_BASE_DELAY = 1.0
_RECONNECT_MAX_DELAY = 60.0


class BislyNATSTransportError(Exception):
    """Error in the NATS transport layer."""


class BislyNATSConnectionError(BislyNATSTransportError):
    """Connection to the NATS server failed."""


def _generate_inbox() -> str:
    """Generate a random NATS inbox ID matching the app's pattern."""
    return "_INBOX." + "".join(random.choices(_INBOX_CHARS, k=_INBOX_LENGTH))


class BislyNATSTransport:
    """Raw NATS text protocol transport over aiohttp WebSocket."""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._inbox: str = _generate_inbox()

        self._sid_counter: int = 1
        self._subscribed: set[str] = set()

        self._pending: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._broadcast_listeners: list[Callable[[dict[str, Any]], Awaitable[None]]] = []
        self._reconnect_callbacks: list[Callable[[], Awaitable[None]]] = []

        self._connected: bool = False
        self._reconnect_attempt: int = 0
        self._receive_task: asyncio.Task[None] | None = None
        self._should_reconnect: bool = True
        self._connection_lock: asyncio.Lock = asyncio.Lock()

    async def connect(self) -> None:
        """Connect to the Bisly NATS WebSocket server."""
        async with self._connection_lock:
            if self._connected:
                return
            self._should_reconnect = True
            await self._connect_ws()

    async def disconnect(self) -> None:
        """Disconnect from the NATS server and clean up."""
        self._should_reconnect = False
        await self._cleanup()

    @property
    def connected(self) -> bool:
        """Whether the transport is currently connected."""
        return self._connected and self._ws is not None and not self._ws.closed

    @property
    def inbox(self) -> str:
        """The current inbox subject."""
        return self._inbox

    def add_broadcast_listener(self, callback: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """Register a callback for broadcast messages."""
        if callback not in self._broadcast_listeners:
            self._broadcast_listeners.append(callback)

    def add_reconnect_callback(self, callback: Callable[[], Awaitable[None]]) -> None:
        """Register a callback invoked after a successful reconnect."""
        if callback not in self._reconnect_callbacks:
            self._reconnect_callbacks.append(callback)

    async def subscribe(self, subject: str) -> None:
        """Subscribe to a NATS subject."""
        if subject not in self._subscribed:
            sid = self._sid_counter
            self._sid_counter += 1
            await self._send_raw(f"SUB {subject} {sid}\r\n")
            self._subscribed.add(subject)
            LOGGER.debug("Subscribed to NATS subject: %s (sid=%d)", subject, sid)

    async def publish(
        self,
        subject: str,
        payload: dict[str, Any],
        request_id: int | None = None,
        timeout: float = 15.0,
    ) -> dict[str, Any] | None:
        """Publish a message to a NATS subject and await the reply if expected.

        Fire-and-forget payloads (action == "set") return None immediately.
        """
        if not self.connected:
            raise BislyNATSConnectionError("NATS transport is not connected")

        json_str = json.dumps(payload)
        msg = f"PUB {subject} {self._inbox} {len(json_str)}\r\n{json_str}\r\n"

        if payload.get("action") == "set":
            await self._ws.send_str(msg)  # type: ignore[union-attr]
            return None

        if request_id is None:
            request_id = int(payload.get("request_id", 0))

        future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._ws.send_str(msg)  # type: ignore[union-attr]
            async with asyncio.timeout(timeout):
                return await future
        finally:
            self._pending.pop(request_id, None)

    async def publish_fire_and_forget(self, subject: str, payload: dict[str, Any]) -> None:
        """Publish a message without registering a reply future."""
        if not self.connected:
            raise BislyNATSConnectionError("NATS transport is not connected")

        json_str = json.dumps(payload)
        msg = f"PUB {subject} {self._inbox} {len(json_str)}\r\n{json_str}\r\n"
        await self._ws.send_str(msg)  # type: ignore[union-attr]

    # ------------------------------------------------------------------
    # Internal methods
    # ------------------------------------------------------------------

    async def _connect_ws(self) -> None:
        """Establish the WebSocket connection and set up NATS protocol."""
        LOGGER.debug("Connecting to Bisly NATS at %s", BISLY_WS_URL)
        try:
            self._ws = await self._session.ws_connect(BISLY_WS_URL)
        except Exception as exc:
            LOGGER.error("Failed to connect to %s: %s", BISLY_WS_URL, exc)
            raise BislyNATSConnectionError(f"Failed to connect: {exc}") from exc

        connect_msg = NATS_CONNECT_TEMPLATE.format(user=BISLY_NATS_USER, password=BISLY_NATS_PASS)
        await self._ws.send_str(f"CONNECT {connect_msg}\r\n")

        try:
            async with asyncio.timeout(10):
                msg = await self._ws.receive()
                if msg.type == WSMsgType.TEXT:
                    if msg.data.startswith("+OK"):
                        LOGGER.debug("NATS connection accepted")
                    elif msg.data.startswith("-ERR"):
                        raise BislyNATSConnectionError(f"NATS connection rejected: {msg.data}")
        except TimeoutError:
            raise BislyNATSConnectionError("NATS CONNECT timed out")  # noqa: B904

        self._inbox = _generate_inbox()
        await self._send_raw(f"SUB {self._inbox} 999\r\n")

        for subject in list(self._subscribed):
            sid = self._sid_counter
            self._sid_counter += 1
            await self._send_raw(f"SUB {subject} {sid}\r\n")

        self._connected = True
        self._reconnect_attempt = 0

        self._receive_task = asyncio.ensure_future(self._receive_loop())

        for cb in self._reconnect_callbacks:
            try:
                await cb()
            except Exception:  # noqa: BLE001
                LOGGER.exception("Error in reconnect callback")

        LOGGER.info("Connected to Bisly NATS (inbox=%s)", self._inbox)

    async def _cleanup(self) -> None:
        """Clean up all tasks and close the WebSocket."""
        self._connected = False

        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
            self._receive_task = None

        for future in self._pending.values():
            if not future.done():
                future.set_exception(BislyNATSConnectionError("Connection closed"))
        self._pending.clear()
        self._subscribed.clear()

        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None

    async def _send_raw(self, raw: str) -> None:
        """Send a raw NATS protocol string."""
        if self._ws and not self._ws.closed:
            await self._ws.send_str(raw)

    async def _receive_loop(self) -> None:
        """Background task that processes incoming WebSocket messages."""
        leftover: str = ""
        while self._connected and self._ws and not self._ws.closed:
            try:
                msg = await self._ws.receive()
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Error receiving from NATS: %s", exc)
                break

            if msg.type in (WSMsgType.TEXT, WSMsgType.BINARY):
                raw = msg.data.decode("utf-8") if isinstance(msg.data, bytes) else msg.data
                LOGGER.debug("NATS raw frame (type=%s): %.200s", msg.type.name, raw)
                leftover = await self._handle_text(leftover + raw)
            elif msg.type in (WSMsgType.CLOSED, WSMsgType.CLOSE, WSMsgType.ERROR):
                LOGGER.warning("NATS WebSocket closed: type=%s", msg.type)
                break

        await self._handle_disconnect()

    async def _handle_text(self, data: str) -> str:
        """Handle incoming NATS text data, possibly spanning multiple frames."""
        while data:
            if data.startswith("PING\r\n"):
                await self._send_raw("PONG\r\n")
                data = data[6:]
                continue

            if data.startswith("-ERR"):
                end = data.index("\r\n") if "\r\n" in data else len(data)
                LOGGER.warning("NATS error: %s", data[:end])
                data = data[end + 2 :] if "\r\n" in data else ""
                continue

            if data.startswith("MSG "):
                header_end = data.index("\r\n") if "\r\n" in data else -1
                if header_end == -1:
                    return data

                header = data[:header_end]
                parts = header.split(" ")
                if len(parts) < 4:
                    LOGGER.debug("Malformed MSG header: %s", header)
                    return ""

                try:
                    payload_size = int(parts[-1])
                except ValueError:
                    LOGGER.debug("Bad MSG size in header: %s", parts[-1])
                    return ""

                data = data[header_end + 2 :]

                if len(data) < payload_size + 2:
                    return header + "\r\n" + data

                payload_str = data[:payload_size]
                data = data[payload_size + 2 :]

                payload = self._parse_json(payload_str)
                if payload is not None:
                    request_id = payload.get("request_id")
                    if request_id is not None and request_id in self._pending:
                        future = self._pending.pop(request_id)
                        if not future.done():
                            future.set_result(payload)
                    else:
                        await self._dispatch_broadcast(payload)
                continue

            if data[0] == "{":
                payload = self._parse_json_lines(data)
                if payload is not None:
                    request_id = payload.get("request_id")
                    if request_id is not None and request_id in self._pending:
                        future = self._pending.pop(request_id)
                        if not future.done():
                            future.set_result(payload)
                    else:
                        await self._dispatch_broadcast(payload)
                    break
                break

            data = data[1:]

        return ""

    def _parse_json(self, data: str) -> dict[str, Any] | None:
        """Parse a JSON object from a string, returning None on failure."""
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            LOGGER.debug("Failed to parse JSON: %s", data[:200])
            return None

    def _parse_json_lines(self, data: str) -> dict[str, Any] | None:
        """Extract a JSON object from raw NATS text lines."""
        for line in data.split("\r\n"):
            stripped = line.strip()
            if stripped.startswith("{"):
                try:
                    return json.loads(stripped)
                except json.JSONDecodeError:
                    continue
        return None

    async def _dispatch_broadcast(self, payload: dict[str, Any] | None) -> None:
        """Dispatch a broadcast message to all registered listeners."""
        if payload is None:
            return
        for listener in self._broadcast_listeners:
            try:
                await listener(payload)
            except Exception:  # noqa: BLE001
                LOGGER.exception("Error in broadcast listener")

    async def _handle_disconnect(self) -> None:
        """Handle unexpected disconnection with reconnection logic."""
        LOGGER.warning("NATS connection lost")

        for future in self._pending.values():
            if not future.done():
                future.set_exception(BislyNATSConnectionError("Connection lost"))
        self._pending.clear()
        if self._ws and not self._ws.closed:
            await self._ws.close()
        self._ws = None
        self._connected = False

        if not self._should_reconnect:
            return

        while self._should_reconnect and self._reconnect_attempt < _MAX_RECONNECT_ATTEMPTS:
            delay = min(_RECONNECT_BASE_DELAY * (2**self._reconnect_attempt), _RECONNECT_MAX_DELAY)
            jittered = delay * (0.5 + random.random())
            self._reconnect_attempt += 1

            LOGGER.debug(
                "Reconnecting in %.1fs (attempt %d/%d)",
                jittered,
                self._reconnect_attempt,
                _MAX_RECONNECT_ATTEMPTS,
            )
            await asyncio.sleep(jittered)

            try:
                await self._connect_ws()
                break
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("Reconnection attempt %d failed: %s", self._reconnect_attempt, exc)

        if not self._connected:
            LOGGER.error("Failed to reconnect after %d attempts", self._reconnect_attempt)
