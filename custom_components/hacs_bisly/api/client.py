"""
API Client for the Bisly smart home system.

This module provides a high-level client that wraps the NATS transport
and implements the Bisly authentication and command protocol.

Three-layer data flow: Entities → Coordinator → API Client.
Only the coordinator should call the API client. Entities must never
import or call the API client directly.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
import json
from typing import TYPE_CHECKING, Any

import aiohttp

from custom_components.hacs_bisly.const import (
    ACTION_GET,
    ACTION_SET,
    CMD_CLIMATE,
    CMD_CONTROLLER_LIST,
    CMD_CURTAINS,
    CMD_LIGHT,
    CMD_RGB,
    CMD_VENTILATION,
    LOGGER,
    SUBJECT_BROADCAST,
    SUBJECT_ROUTING,
)

from .nats_transport import BislyNATSConnectionError, BislyNATSTransport

if TYPE_CHECKING:
    from collections.abc import Awaitable


class BislyApiClientError(Exception):
    """Base exception to indicate a general API error."""


class BislyApiClientCommunicationError(BislyApiClientError):
    """Exception to indicate a communication error with the API."""


class BislyApiClientAuthenticationError(BislyApiClientError):
    """Exception to indicate an authentication error with the API."""


class BislyApiClient:
    """
    High-level client for the Bisly smart home system.

    Wraps the raw NATS transport and implements:
    - Authentication (handshake)
    - Server discovery
    - Command execution with proper envelope construction
    - Broadcast message handling

    Usage:
        client = BislyApiClient(username="myuser", password="secret", session=session)
        await client.authenticate()
        await client.connect(on_broadcast=my_callback)
        response = await client.get_rooms()
        await client.disconnect()
    """

    def __init__(
        self,
        username: str,
        password: str,
        session: aiohttp.ClientSession,
    ) -> None:
        """Initialize the API client.

        Args:
            username: The user's Bisly account username.
            password: The user's Bisly account password.
            session: The aiohttp ClientSession for HTTP/WebSocket requests.
        """
        self._username = username
        self._password = password
        self._transport = BislyNATSTransport(session)

        # Authentication state
        self._auth_hash: str = ""
        self._user_id: int = 0
        self._server_id: str = ""
        self._request_counter: int = 2  # Match the app's starting value
        self._ui_version: str = "6.7.3"
        self._handshake_config: str = ""
        self._authenticated: bool = False
        self._broadcast_subject: str = ""

    @property
    def session(self) -> aiohttp.ClientSession:
        """The aiohttp ClientSession used by this client."""
        return self._transport._session  # noqa: SLF001

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def authenticate(self) -> dict[str, Any]:
        """
        Authenticate with the Bisly system.

        Sends a handshake command to cloud.auth with username/password.
        On success, stores auth_hash, user_id, and server_id for subsequent use.

        Returns:
            The handshake response dictionary.

        Raises:
            BislyApiClientAuthenticationError: If authentication fails.
            BislyApiClientCommunicationError: If communication fails.
        """
        LOGGER.debug("Authenticating to Bisly as %s", self._username)

        # Connect transport if not already connected
        if not self._transport.connected:
            await self._transport.connect()

        # Send handshake on cloud.auth (routed via _subject_for)
        handshake_msg = {
            "command": "handshake",
            "username": self._username,
            "password": self._password,
            "action": "get",
            "user_id": None,  # Not set for handshake
        }

        try:
            response = await self._request(handshake_msg, timeout=30.0)
        except (BislyNATSConnectionError, TimeoutError) as exc:
            raise BislyApiClientCommunicationError(f"Authentication handshake failed: {exc}") from exc

        if response is None:
            raise BislyApiClientAuthenticationError("Authentication failed: no response from server")

        # Check for successful authentication (matching the working script)
        if response.get("param") == "authenticated" and response.get("auth_hash"):
            self._auth_hash = response["auth_hash"]
            self._user_id = int(response.get("user_id", 0))
            self._server_id = response.get("serverID") or ""
            self._handshake_config = response.get("config", "")
            self._authenticated = True
        else:
            raise BislyApiClientAuthenticationError(f"Authentication failed: {response}")

        LOGGER.info(
            "Authenticated to Bisly (user_id=%d, server_id=%s)",
            self._user_id,
            self._server_id,
        )
        return response

    async def connect(self, on_broadcast: Callable[[dict[str, Any]], Awaitable[None]]) -> None:
        """
        Connect to Bisly and subscribe to broadcast state updates.

        Args:
            on_broadcast: Async callback invoked for each incoming broadcast message.
        """
        if not self._authenticated:
            await self.authenticate()

        if not self._transport.connected:
            await self._transport.connect()

        # Register broadcast listener
        self._transport.add_broadcast_listener(on_broadcast)

        # Subscribe to broadcast.<serverID> (matching the working script)
        self._broadcast_subject: str = ""
        if self._server_id:
            self._broadcast_subject = f"{SUBJECT_BROADCAST}.{self._server_id}"
            await self._transport.subscribe(self._broadcast_subject)
            LOGGER.debug("Subscribed to broadcasts: %s", self._broadcast_subject)

        # Register reconnect callback to re-subscribe broadcasts after reconnect
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

    # ------------------------------------------------------------------
    # Core request/response
    # ------------------------------------------------------------------

    async def _request(self, payload: dict[str, Any], timeout: float = 20.0) -> dict[str, Any] | None:
        """
        Send a request and wait for the reply.

        Adds auth_hash, user_id, serverID automatically (except for handshake).
        Uses _subject_for to route the command to the correct NATS subject.

        Args:
            payload: The command payload.
            timeout: Timeout in seconds.

        Returns:
            The response dictionary, or None for fire-and-forget.
        """
        req_id = self._next_id()
        payload["request_id"] = req_id

        # Add auth fields for non-handshake commands
        if payload.get("command") != "handshake":
            payload["auth_hash"] = self._auth_hash
            payload["user_id"] = self._user_id
        if "serverID" not in payload:
            payload["serverID"] = self._server_id

        subject = self._subject_for(payload)
        LOGGER.debug("API request: %s → %s", payload.get("command"), subject)
        result = await self._transport.publish(subject, payload, req_id, timeout)
        LOGGER.debug("API response: %s → %s...", payload.get("command"), json.dumps(result)[:200] if result else "None")
        return result

    def _next_id(self) -> int:
        """Generate the next request ID."""
        self._request_counter += 1
        return self._request_counter

    @staticmethod
    def _subject_for(payload: dict[str, Any]) -> str:
        """
        Route a command payload to the correct NATS subject.

        Matches the working script's _subject_for logic:
        - handshake → cloud.auth
        - servers → cloud.servers
        - user_settings → commands.cloud
        - user_email → cloud.email
        - account → account.{serverID}
        - log → log.{serverID}
        - everything else → commands.{serverID}
        """
        cmd = payload.get("command", "")
        sid = str(payload.get("serverID", ""))

        if cmd in SUBJECT_ROUTING:
            return SUBJECT_ROUTING[cmd]

        if cmd == "account":
            return f"account.{sid}"
        if cmd == "log":
            return f"log.{sid}"
        if cmd == "videoserver":
            return f"videoserver.{sid}"

        return f"commands.{sid}"

    # ------------------------------------------------------------------
    # Data fetching
    # ------------------------------------------------------------------

    async def get_rooms(self) -> dict[str, Any] | None:
        """Fetch the list of rooms."""
        return await self._request({"command": "rooms", "action": ACTION_GET})

    async def get_room_devices(self, room_id: int, controller_type: str) -> dict[str, Any] | None:
        """
        Fetch devices of a given controller type for a specific room.

        Args:
            room_id: The room ID.
            controller_type: Controller type code ("1"=lights, "3"=climate, etc.).
        """
        return await self._request(
            {
                "command": CMD_CONTROLLER_LIST,
                "action": ACTION_GET,
                "type": controller_type,
                "param": str(room_id),
            }
        )

    async def get_global_controller(self, controller_type: str) -> dict[str, Any] | None:
        """Fetch global controllers (ventilation, counters, scenarios)."""
        return await self._request(
            {
                "command": CMD_CONTROLLER_LIST,
                "action": ACTION_GET,
                "type": controller_type,
                "param": "0",
            }
        )

    async def get_cameras(self) -> dict[str, Any] | None:
        """Fetch the list of cameras (controller_list type 14 = VIDEOPHONE)."""
        return await self._request(
            {
                "command": CMD_CONTROLLER_LIST,
                "action": ACTION_GET,
                "type": "14",
            }
        )

    # ------------------------------------------------------------------
    # Device control — mirroring the working script's _set_device pattern
    # ------------------------------------------------------------------

    async def _set_device(
        self,
        cmd_type: str,
        device: dict[str, Any],
        param_override: str = "",
        **extra: Any,
    ) -> dict[str, Any] | None:
        """
        Send a device control command.

        Mirrors the working script's _set_device: copies every device field
        (address as array, id, type, sw, version, label, etc.) into the payload.

        Args:
            cmd_type: The command type (e.g. "light", "climate").
            device: The full device dict from controller_list.
            param_override: Value to override device's current param/state.
            **extra: Additional fields to include in the payload.
        """
        payload: dict[str, Any] = {"action": ACTION_SET, "command": cmd_type}

        # Copy standard device fields
        for key in ("address2", "loop", "type", "sw", "version", "label", "id"):
            if key in device:
                payload[key] = device[key]

        # address is sent as an array of strings (matching the app)
        addr = device.get("address")
        if addr is not None:
            payload["address"] = [str(addr), str(device.get("address2", ""))]

        # param: use override or device's current state
        payload["param"] = param_override or str(device.get("param", ""))

        payload.update(extra)
        return await self._request(payload)

    async def toggle_light(self, device: dict[str, Any], on: bool) -> None:
        """Toggle a light on or off.

        Args:
            device: The full device dict from the controller_list response.
            on: True to turn on, False to turn off.
        """
        param = "1" if on else "0"
        await self._set_device(CMD_LIGHT, device, param_override=param)

    async def set_rgb(self, device: dict[str, Any], red: int, green: int, blue: int) -> dict[str, Any] | None:
        """Set RGB color on a lighting device.

        Args:
            device: The full device dict.
            red: Red value (0-255).
            green: Green value (0-255).
            blue: Blue value (0-255).
        """
        return await self._set_device(
            CMD_RGB,
            device,
            red=red,
            green=green,
            blue=blue,
        )

    async def set_climate(
        self, device: dict[str, Any], temp: float | None = None, mode: str | None = None
    ) -> dict[str, Any] | None:
        """Set climate target temperature or mode.

        Args:
            device: The full device dict.
            temp: Target temperature to set (optional).
            mode: Climate mode to set (optional).
        """
        if temp is not None:
            return await self._set_device(CMD_CLIMATE, device, param_override=str(temp))
        if mode is not None:
            return await self._set_device(CMD_CLIMATE, device, param_override=mode)
        return None

    async def set_curtain(self, device: dict[str, Any], position: str = "0") -> dict[str, Any] | None:
        """Set curtain position.

        Args:
            device: The full device dict.
            position: Position value ("0" = closed, "1" = open, etc.).
        """
        return await self._set_device(CMD_CURTAINS, device, param_override=position)

    async def set_ventilation(self, device: dict[str, Any], speed: str) -> dict[str, Any] | None:
        """Set ventilation speed.

        Args:
            device: The full device dict.
            speed: Speed value.
        """
        return await self._set_device(CMD_VENTILATION, device, param_override=speed)

    # ------------------------------------------------------------------
    # WebRTC videoserver commands (WebRTC streaming)
    # ------------------------------------------------------------------

    async def open_videoserver(self, camera_id: str) -> dict[str, Any] | None:
        """Open a WebRTC video session with the Bisly videoserver.

        Sends an open request to receive an SDP offer from the server.
        Matches the Bisly app's getVideoOffer() pattern.

        Args:
            camera_id: The camera UUID.
        """
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
        """Send the local SDP answer back to the videoserver.

        Args:
            connection_id: The connection ID from the open response.
            sdp_base64: Base64-encoded JSON SDP answer.
        """
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
        """Send a local ICE candidate to the videoserver.

        Args:
            connection_id: The connection ID from the open response.
            candidate: The ICE candidate string.
            sdp_mid: The media stream identification.
            sdp_mline_index: The media line index.
        """
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
        """Close the videoserver connection.

        Args:
            connection_id: The connection ID from the open response.
        """
        return await self._request(
            {
                "command": "videoserver",
                "action": "set",
                "type": "close",
                "id": connection_id,
                "param": f"CLOSE,{connection_id}",
            }
        )

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def handshake_config(self) -> str:
        """The handshake config JSON string with camera UUIDs."""
        return self._handshake_config

    @property
    def auth_hash(self) -> str:
        """The authentication hash from the handshake."""
        return self._auth_hash

    @property
    def user_id(self) -> int:
        """The user ID from authentication."""
        return self._user_id

    @property
    def server_id(self) -> str:
        """The current server ID."""
        return self._server_id
