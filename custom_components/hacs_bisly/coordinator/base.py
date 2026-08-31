"""
Push-based DataUpdateCoordinator for hacs_bisly.

This coordinator uses the cloud_push pattern: it connects to the Bisly NATS
WebSocket on setup, subscribes to broadcast.<serverID>, and routes incoming
state updates to entities via async_set_updated_data().

Unlike polling-based coordinators, there is no periodic _async_update_data()
call. Instead, incoming NATS broadcast messages trigger state updates.

Architecture:
    1. _async_setup()                 → authenticate, connect, subscribe
    2. _on_broadcast(message)         → parse, merge, push to entities
    3. async_async_update_data()      → fallback: returns cached data
    4. async_shutdown()               → disconnect NATS transport
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import datetime
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from custom_components.hacs_bisly.api import (
    BislyApiClientAuthenticationError,
    BislyApiClientCommunicationError,
    BislyApiClientError,
)
from custom_components.hacs_bisly.const import (
    BROADCAST_CALL_END,
    BROADCAST_CALL_ENDED,
    BROADCAST_CALL_ENDED_ELSEWHERE,
    BROADCAST_DOOR_NOTIFICATION,
    BROADCAST_DOORBELL,
    BROADCAST_INTERCOM_CALL,
    CAMERA_REFRESH_INTERVAL_SECONDS,
    DEVICE_REFRESH_INTERVAL_SECONDS,
    LOGGER,
)
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

INTERCOM_BROADCAST_TYPES = frozenset(
    {
        BROADCAST_INTERCOM_CALL,
        BROADCAST_CALL_ENDED,
        BROADCAST_CALL_ENDED_ELSEWHERE,
        BROADCAST_CALL_END,
        BROADCAST_DOORBELL,
        BROADCAST_DOOR_NOTIFICATION,
    }
)

if TYPE_CHECKING:
    from custom_components.hacs_bisly.data import BislyConfigEntry
    from homeassistant.core import HomeAssistant


class BislyDataUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """
    Push-based coordinator for the Bisly smart home integration.

    Manages the persistent NATS WebSocket connection, authenticates with the
    Bisly cloud, and distributes real-time broadcast messages to all entities.

    Attributes:
        config_entry: The config entry for this integration instance.
    """

    config_entry: BislyConfigEntry

    # How long (seconds) a pending-command entry stays valid before expiry.
    # Must be long enough for a full NATS roundtrip (send → broadcast-receive),
    # short enough to avoid accidentally matching an unrelated later broadcast.
    PENDING_COMMAND_TTL = 5.0

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        logger: logging.Logger,
        name: str,
        config_entry: BislyConfigEntry,
        update_interval: datetime.timedelta | None = None,
        always_update: bool = False,
    ) -> None:
        """Initialize the coordinator."""
        self.config_entry = config_entry
        self._initial_data_ready = asyncio.Event()
        self._last_camera_refresh: float = 0.0
        self._last_device_refresh: float = 0.0
        # Track pending device commands so broadcast acks can be routed to the
        # correct (room_id, id, sw) entry when id/address alone is ambiguous.
        # Keys are (int(id), int(address)), values are dicts with keys:
        #   room_id, sw, expires_at (monotonic timestamp)
        self._pending_commands: dict[tuple[int, int], dict[str, Any]] = {}
        super().__init__(
            hass,
            logger,
            name=name,
            config_entry=config_entry,
            update_interval=update_interval,
            always_update=always_update,
        )

    async def _async_setup(self) -> None:
        """
        Set up the coordinator.

        Called automatically during async_config_entry_first_refresh().
        Performs authentication, NATS connection, and broadcast subscription.

        Raises:
            ConfigEntryAuthFailed: If authentication fails.
            UpdateFailed: If connection setup fails.
        """
        client = self.config_entry.runtime_data.client
        LOGGER.debug("Setting up Bisly coordinator")

        try:
            # Authenticate with the Bisly cloud
            await client.authenticate()
        except BislyApiClientAuthenticationError as exc:
            raise ConfigEntryAuthFailed(
                translation_domain="hacs_bisly",
                translation_key="authentication_failed",
            ) from exc
        except BislyApiClientCommunicationError as exc:
            raise UpdateFailed(
                translation_domain="hacs_bisly",
                translation_key="auth_communication_failed",
            ) from exc

        # Connect to NATS and subscribe to broadcasts
        try:
            await client.connect(self._on_broadcast)
        except BislyApiClientError as exc:
            raise UpdateFailed(
                translation_domain="hacs_bisly",
                translation_key="connection_failed",
            ) from exc

        LOGGER.info(
            "Bisly coordinator connected (user_id=%d, server_id=%s)",
            client.user_id,
            client.server_id,
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """
        Fetch state data.

        On first refresh: queries rooms + controller_list for full device state.
        Subsequent refreshes: re-fetches controller_list to sync state from
        other clients (Bisly app). Runs on a polling timer.

        Returns:
            The state dictionary with device lists.
        """
        if not self._initial_data_ready.is_set():
            client = self.config_entry.runtime_data.client

            # Step 1: get room list (matching reference script)
            try:
                rooms_resp = await client.get_rooms()
            except BislyApiClientError as exc:
                LOGGER.debug("get_rooms failed: %s", exc)
                self._initial_data_ready.set()
                return self.data or {}

            room_entries = rooms_resp.get("rooms") or rooms_resp.get("param") or []
            if isinstance(room_entries, dict):
                room_entries = room_entries.get("rooms") or room_entries.get("list") or []
            if not isinstance(room_entries, list):
                LOGGER.debug("Unexpected rooms response format: %s", type(room_entries).__name__)
                self._initial_data_ready.set()
                return self.data or {}

            # Collect room IDs that we need to fetch devices for
            room_ids: list[int] = []
            for room in room_entries:
                if isinstance(room, dict):
                    room_id = room.get("id")
                    if room_id is not None:
                        room_ids.append(room_id)

            # Collect all devices across rooms. The initial fetch must be
            # sequential — flooding a fresh connection with concurrent NATS
            # requests causes mass timeouts because the server serialises
            # processing and the later requests starve within the 15 s window.
            all_devices: list[dict[str, Any]] = []
            for room_id in room_ids:
                for ctrl_type in ("1", "3"):
                    try:
                        resp = await client.get_room_devices(room_id, ctrl_type)
                        if resp and isinstance(resp.get("param"), dict):
                            devices = _extract_devices_from_param(resp["param"])
                            for d in devices:
                                d["_ctrl_type"] = ctrl_type
                            all_devices.extend(devices)
                    except (BislyApiClientError, TimeoutError) as exc:
                        LOGGER.debug("room_id=%s type=%s failed: %s", room_id, ctrl_type, exc)

            # Add rooms from the initial room list
            rooms_list: list[dict[str, Any]] = []
            seen_room_ids: set[int] = set()
            for room in room_entries:
                if isinstance(room, dict):
                    rid = room.get("id")
                    if rid is not None and rid not in seen_room_ids:
                        seen_room_ids.add(rid)
                        rooms_list.append(
                            {
                                "id": rid,
                                "label": room.get("label", room.get("name", "")),
                            }
                        )

            result = normalize_device_lists(all_devices)
            result["rooms"] = rooms_list or result.get("rooms", [])

            # Fetch camera list (global controller type 14)
            try:
                cameras_resp = await client.get_cameras()
                cameras_raw = _extract_cameras_from_response(cameras_resp)
                if cameras_raw:
                    _attach_camera_uuids(cameras_raw, client.handshake_config)
                    result["cameras"] = cameras_raw
                    LOGGER.debug("Fetched %d cameras", len(cameras_raw))
                self._last_camera_refresh = time.monotonic()
            except BislyApiClientError as exc:
                LOGGER.debug("get_cameras failed: %s", exc)

            self._last_device_refresh = time.monotonic()
            LOGGER.debug("Initial fetch complete: %d devices across %d rooms", len(all_devices), len(rooms_list))
            self._initial_data_ready.set()
            return result

        # Subsequent refreshes: the room/device list rarely changes, so it is
        # only re-fetched once DEVICE_REFRESH_INTERVAL_SECONDS has elapsed.
        # Broadcasts already keep state in sync for HA-initiated changes and
        # for most Bisly-app / physical-switch changes; this poll just catches
        # anything broadcasts might have missed.
        client = self.config_entry.runtime_data.client
        now = time.monotonic()
        if now - self._last_device_refresh >= DEVICE_REFRESH_INTERVAL_SECONDS:
            result = await self._refresh_devices(client)
            self._last_device_refresh = now
        else:
            result = {**(self.data or {})}

        return await self._refresh_cameras(client, result)

    async def _refresh_devices(self, client: Any) -> dict[str, Any]:
        """Re-fetch controller_list for all known rooms to sync device state."""
        rooms = (self.data or {}).get("rooms", [])
        if not rooms:
            return self.data or {}

        all_devices: list[dict[str, Any]] = []

        async def _fetch_room(room_id: int) -> None:
            for ctrl_type in ("1", "3"):
                try:
                    resp = await client.get_room_devices(room_id, ctrl_type)
                    if resp and isinstance(resp.get("param"), dict):
                        devices = _extract_devices_from_param(resp["param"])
                        for d in devices:
                            d["_ctrl_type"] = ctrl_type
                        all_devices.extend(devices)
                except (BislyApiClientError, TimeoutError) as exc:
                    LOGGER.debug("room_id=%s type=%s refresh failed: %s", room_id, ctrl_type, exc)

        # Poll serially on the warm connection — one request at a time to avoid
        # ws frame interleaving.  Even 2 concurrent requests on a cold connection
        # cause timeouts (the server serialises processing).
        for room in rooms:
            room_id = room.get("id")
            if room_id is not None:
                await _fetch_room(room_id)

        result: dict[str, Any] = {}
        if all_devices:
            result = normalize_device_lists(all_devices)
            result["rooms"] = rooms  # Preserve room labels from initial fetch
        else:
            result = {**(self.data or {})}

        return result

    async def _refresh_cameras(self, client: Any, result: dict[str, Any]) -> dict[str, Any]:
        """Refresh the camera list if CAMERA_REFRESH_INTERVAL_SECONDS has elapsed.

        Cameras rarely change, so this is gated independently of the room/device
        refresh — cameras keep their own (currently identical) refresh cadence.
        """
        now = time.monotonic()
        if now - self._last_camera_refresh > CAMERA_REFRESH_INTERVAL_SECONDS:
            try:
                cameras_resp = await client.get_cameras()
                cameras_raw = _extract_cameras_from_response(cameras_resp)
                if cameras_raw:
                    result["cameras"] = cameras_raw
                else:
                    result["cameras"] = (self.data or {}).get("cameras", [])
                self._last_camera_refresh = now
            except BislyApiClientError as exc:
                LOGGER.debug("get_cameras refresh failed: %s", exc)
                result["cameras"] = (self.data or {}).get("cameras", [])
        else:
            result["cameras"] = (self.data or {}).get("cameras", [])

        return result

    async def shutdown(self) -> None:
        """Shut down the coordinator — disconnect from NATS."""
        client = self.config_entry.runtime_data.client
        try:
            await client.disconnect()
        except BislyApiClientError:
            LOGGER.debug("Error during coordinator shutdown", exc_info=True)

    # ------------------------------------------------------------------
    # Device control — entities route through the coordinator, not the API
    # client directly, so pending-command tracking can disambiguate
    # broadcast acks when id/address alone is not unique across rooms.
    # ------------------------------------------------------------------

    def _cleanup_expired_pending_commands(self) -> None:
        """Remove expired pending-command entries."""
        now = time.monotonic()
        expired = [k for k, v in self._pending_commands.items() if v.get("expires_at", 0) < now]
        for k in expired:
            del self._pending_commands[k]

    async def async_set_light(self, device_data: dict[str, Any], on: bool) -> None:
        """
        Toggle a light on or off, tracking the pending command for broadcast ack routing.

        Records a short-lived pending entry so that when the SET acknowledgement
        arrives (with only id/address, no room_id/sw), parse_broadcast_message can
        update the correct (room_id, id, sw) entry instead of all entries sharing
        that id.

        Args:
            device_data: The full device dict from the controller_list response.
            on: True to turn on, False to turn off.
        """
        self._cleanup_expired_pending_commands()
        self._register_pending_command(device_data)

        client = self.config_entry.runtime_data.client
        await client.toggle_light(device_data, on=on)

    async def async_set_brightness(self, device_data: dict[str, Any], brightness_pct: int) -> None:
        """Set light brightness (0-100), tracking pending command for ack routing.

        Args:
            device_data: The full device dict from the controller_list response.
            brightness_pct: Brightness percentage (0-100).
        """
        self._cleanup_expired_pending_commands()
        self._register_pending_command(device_data)

        client = self.config_entry.runtime_data.client
        await client._set_device("light", device_data, param_override=str(brightness_pct))  # noqa: SLF001

    async def async_set_rgb(self, device_data: dict[str, Any], red: int, green: int, blue: int) -> None:
        """Set light RGB color, tracking pending command for ack routing.

        Args:
            device_data: The full device dict from the controller_list response.
            red: Red component (0-255).
            green: Green component (0-255).
            blue: Blue component (0-255).
        """
        self._cleanup_expired_pending_commands()
        self._register_pending_command(device_data)

        client = self.config_entry.runtime_data.client
        await client.set_rgb(device_data, red, green, blue)

    async def async_set_climate(
        self, device_data: dict[str, Any], temp: float | None = None, mode: str | None = None
    ) -> None:
        """Set climate temperature or mode.

        Args:
            device_data: The full device dict.
            temp: Target temperature to set (optional).
            mode: Climate mode to set (optional).
        """
        self._cleanup_expired_pending_commands()

        client = self.config_entry.runtime_data.client
        await client.set_climate(device_data, temp=temp, mode=mode)

    async def async_set_door(self, device_data: dict[str, Any], state: str) -> None:
        """Set door state (lock/unlock).

        Args:
            device_data: The full device dict from the controller_list response.
            state: "0" to lock, "1" to unlock.
        """
        self._cleanup_expired_pending_commands()
        self._register_pending_command(device_data)

        client = self.config_entry.runtime_data.client
        await client.set_door(device_data, state=state)

    async def async_hangup_intercom(self) -> None:
        """Hang up the active intercom call."""
        await self.config_entry.runtime_data.intercom.hangup()

    async def async_open_door_intercom(self, action_id: str) -> None:
        """Execute a door-open action from the active intercom call.

        Args:
            action_id: The action ID from fono GET_DOORS.
        """
        await self.config_entry.runtime_data.intercom.open_door(action_id)

    def _register_pending_command(self, device_data: dict[str, Any]) -> None:
        """Register a pending command entry for broadcast ack routing."""
        device_id_raw = device_data.get("id")
        address_raw = device_data.get("address")
        device_room_id = device_data.get("room_id")
        device_sw = device_data.get("sw")

        if device_id_raw is not None and address_raw is not None:
            try:
                key = (int(device_id_raw), int(address_raw))
            except ValueError, TypeError:
                return
            if key is not None and device_room_id is not None:
                self._pending_commands[key] = {
                    "room_id": device_room_id,
                    "sw": device_sw,
                    "expires_at": time.monotonic() + self.PENDING_COMMAND_TTL,
                }
                LOGGER.debug(
                    "Pending command: id=%s address=%s -> room_id=%s sw=%s",
                    device_id_raw,
                    address_raw,
                    device_room_id,
                    device_sw,
                )

    # ------------------------------------------------------------------
    # Broadcast handling
    # ------------------------------------------------------------------

    async def _on_broadcast(self, message: dict[str, Any]) -> None:
        """
        Handle an incoming NATS broadcast message.

        Parses the message and pushes the updated state to all entities.

        Args:
            message: The parsed JSON broadcast message from the Bisly server.
        """
        LOGGER.debug("Received broadcast: %s keys=%s", message.get("command", "unknown"), list(message.keys())[:10])

        # Clean up expired entries before consulting the pending map
        self._cleanup_expired_pending_commands()

        # Merge the broadcast delta into the current data
        current = self.data or {}
        delta = parse_broadcast_message(message, current, self._pending_commands)
        if delta:
            LOGGER.debug("Broadcast delta: %s", {k: v for k, v in delta.items() if k != "_last_broadcast"})
            updated = _deep_merge(current, delta)
            self.async_set_updated_data(updated)
        else:
            LOGGER.debug("Broadcast returned no delta")

        # Forward videoserver ICE candidates to active camera sessions
        if message.get("type") == "ice" and message.get("command") == "videoserver":
            self._handle_videoserver_ice(message)

        # Forward intercom-related broadcasts to the intercom manager
        btype = str(message.get("type", ""))
        if btype in INTERCOM_BROADCAST_TYPES or message.get("command") == "fono":
            await self.config_entry.runtime_data.intercom.handle_broadcast(message)

    def _handle_videoserver_ice(self, message: dict[str, Any]) -> None:
        """Forward videoserver ICE candidates to active WebRTC camera sessions."""
        # Import here to avoid circular
        from custom_components.hacs_bisly.camera import _ACTIVE_SESSIONS  # noqa: PLC0415

        connection_id = str(message.get("id", ""))
        if not connection_id:
            return

        param = message.get("param", "")
        if not param:
            return

        try:
            candidate_json = json.loads(base64.b64decode(param).decode())
        except json.JSONDecodeError, UnicodeDecodeError:
            LOGGER.debug("Failed to decode videoserver ICE candidate: %s", param)
            return

        for session in list(_ACTIVE_SESSIONS.values()):
            if getattr(session, "_bid", "") == connection_id:
                _ = asyncio.ensure_future(session.handle_bisly_ice(candidate_json))
                return

        # Signal that we have received initial device data
        if not self._initial_data_ready.is_set():
            data = self.data or {}
            # Check if any device list key is populated
            for key in ("lights", "climate_zones", "rooms", "doors", "curtains", "ventilation", "saunas"):
                if data.get(key):
                    self._initial_data_ready.set()
                    LOGGER.debug("Initial broadcast data received with %d %s", len(data[key]), key)
                    break


# ------------------------------------------------------------------
# Data parsing functions
# ------------------------------------------------------------------


def parse_rooms_response(response: dict[str, Any]) -> dict[str, Any]:
    """
    Parse the initial rooms response into a simple room list and device map.

    The server response for `get_rooms()` returns:
        {"rooms": [{"id": 10036, "label": "Wohnzimmer", ...}, ...]}

    The server response for `controller_list` per room returns:
        {"param": {"10036": {"items": [{...}, ...], "climate": [{...}, ...]}}}

    After fetching all per-room devices, normalize into grouped lists for entity platforms.
    """
    # --- Case 1: rooms list (from get_rooms) ---
    rooms_list = response.get("rooms", response.get("param", []))
    if (
        isinstance(rooms_list, list)
        and rooms_list
        and isinstance(rooms_list[0], dict)
        and ("id" in rooms_list[0] or "label" in rooms_list[0])
    ):
        rooms: list[dict[str, Any]] = [
            {"id": r.get("id"), "label": r.get("label", r.get("name", ""))} for r in rooms_list if isinstance(r, dict)
        ]
        return {"rooms": rooms}

    # --- Case 2: controller_list per-room response ---
    param = response.get("param")
    if isinstance(param, dict):
        devices = _extract_devices_from_param(param)
        if devices:
            return normalize_device_lists(devices)

    return {}


def _extract_devices_from_param(param: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract a flat device list from the nested param.{room_id}.{items|climate} structure.

    Matches the reference script's _extract_devices().
    """
    all_devices: list[dict[str, Any]] = []

    for room_id_str, room_data in param.items():
        if not isinstance(room_data, dict):
            continue

        room_id = int(room_id_str) if room_id_str.isdigit() else 0

        for key in ("items", "climate", "controller_list"):
            items = room_data.get(key)
            if isinstance(items, list):
                for device in items:
                    if isinstance(device, dict):
                        device["_room_id"] = room_id
                    all_devices.append(device)

    if not all_devices:
        # Try flat list
        if isinstance(param, list):
            return param

    return all_devices


def _extract_cameras_from_param(param: dict[str, Any]) -> list[dict[str, Any]]:
    """
    Extract flat camera list from a get_cameras controller_list response
    where param is a dict keyed by camera ID (legacy format).
    """
    cameras: list[dict[str, Any]] = []
    for cam_id_str, cam_data in param.items():
        if not isinstance(cam_data, dict):
            continue
        entry: dict[str, Any] = dict(cam_data)
        entry["camera_id"] = cam_id_str
        cameras.append(entry)
    return cameras


def _extract_cameras_from_response(response: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """
    Extract flat camera list from a get_cameras API response.

    Handles both response formats observed in the wild:
      - param is a list of camera objects: [{"id": "76", "label": "...", ...}, ...]
      - param is a dict keyed by camera ID (legacy/alternate format)

    Returns None if no valid camera data is found.
    """
    if not response:
        return None

    param = response.get("param")
    if not isinstance(param, (list, dict)):
        return None

    # Case 1: param is a flat list of camera objects
    if isinstance(param, list):
        cameras: list[dict[str, Any]] = []
        for entry in param:
            if isinstance(entry, dict):
                cameras.append(dict(entry))
        return cameras

    # Case 2: param is a dict keyed by camera ID (legacy format)
    return _extract_cameras_from_param(param)


def _attach_camera_uuids(cameras: list[dict[str, Any]], handshake_config: str) -> None:
    """Resolve camera UUIDs from the handshake config and attach to the camera dicts.

    The camera controller_list (type 14) returns local numeric ids (e.g. "76").
    The CDN image endpoint requires the real UUID from the handshake config
    (e.g. "53d29e0b-51ba-11f0-96be-0242ac120003").  We extract UUIDs from the
    favorites section and match by position (the only reliable mapping available).

    When a UUID is found, it is stored as "camera_uuid" on the camera dict.
    """
    if not cameras or not handshake_config:
        return

    uuids: list[str] = []

    def _find(obj: Any) -> None:
        if isinstance(obj, dict):
            if "cameraId" in obj:
                uuids.append(obj["cameraId"])
            for v in obj.values():
                _find(v)
        elif isinstance(obj, list):
            for item in obj:
                _find(item)

    try:
        parsed = json.loads(handshake_config)
        _find(parsed)
    except json.JSONDecodeError, TypeError:
        return

    if not uuids:
        return

    # Match by position — the favorites list and the cameras list have the same
    # natural ordering (first favorite → first camera, etc.).
    for i, camera in enumerate(cameras):
        if i < len(uuids):
            camera["camera_uuid"] = uuids[i]


def normalize_device_lists(devices: list[dict[str, Any]]) -> dict[str, Any]:
    """
    Normalize a flat device list into grouped lists by kind.

    Matches the device types from the reference script:
      type "1"  = lights
      type "3"  = climate
      type "28" = floor heating
      type "4"  = ventilation
      type "6"  = curtains
      type "11" = video door
      type "15" = scenarios
    """
    rooms: dict[int, dict[str, Any]] = {}
    lights: list[dict[str, Any]] = []
    climate_zones: list[dict[str, Any]] = []
    curtains: list[dict[str, Any]] = []
    ventilation_devices: list[dict[str, Any]] = []
    doors: list[dict[str, Any]] = []

    for device in devices:
        room_id = device.pop("_room_id", 0)
        ctrl_type = device.pop("_ctrl_type", "")
        device_type = str(device.get("type", ""))

        # Track rooms (IDs seen during device fetch may not all be in initial rooms list)
        if room_id and room_id not in rooms:
            rooms[room_id] = {"id": room_id, "label": ""}

        normalized = {
            "id": device.get("id"),
            "label": device.get("label", ""),
            "room_id": room_id,
            "type": device_type,
            "sw": device.get("sw"),
            "param": device.get("param"),
            "address": device.get("address"),
            "address2": device.get("address2"),
            "version": device.get("version"),
            "loop": device.get("loop"),
        }

        # Map Bisly device types to our categories
        # Climate entries come from ctrl_type "3" and have no "id" — skip for lights
        if ctrl_type == "3" or device_type in ("2", "28") or (device_type == "1" and device.get("id") is None):
            # Climate zone (has temp/sensor fields) or floor heating
            if device.get("temp") is not None or device.get("actual_temp") is not None or device_type == "28":
                normalized["current_temp"] = device.get("actual_temp")
                normalized["target_temp"] = device.get("temp")
                normalized["min_temp"] = device.get("min")
                normalized["max_temp"] = device.get("max")
                normalized["mode"] = device.get("activated")
                normalized["sensor"] = device.get("sensor")
                if device_type == "28":
                    normalized["floor_heating_active"] = device.get("floor_heating_active")
                    normalized["air_climate_status"] = device.get("air_climate_status")
                climate_zones.append(normalized)
            else:
                doors.append(normalized)
        elif device_type in ("1", "0"):  # light types (only from ctrl_type "1")
            # Skip entries without id/label — these are phantom entries
            if normalized.get("id") is not None:
                lights.append(normalized)
        elif device_type == "6":  # curtains
            curtains.append(normalized)
        elif device_type == "4":  # ventilation
            ventilation_devices.append(normalized)
        elif device_type == "11":  # video door
            doors.append(normalized)

    # Cross-reference: copy air_climate_status from floor heating (type 28)
    # to thermostat zones (type 1/2) in the same room.  The thermostat's
    # "activated" field is just the configured mode — the actual running
    # state lives on the sibling type 28 zone.
    floor_heating_status: dict[int, Any] = {}
    for zone in climate_zones:
        if str(zone.get("type")) == "28":
            floor_heating_status[zone["room_id"]] = zone.get("air_climate_status")

    for zone in climate_zones:
        if str(zone.get("type")) in ("1", "2") and zone["room_id"] in floor_heating_status:
            zone["air_climate_status"] = floor_heating_status[zone["room_id"]]

    LOGGER.debug(
        "Normalized: %d rooms, %d lights, %d climates, %d curtains, %d ventilation, %d doors",
        len(rooms),
        len(lights),
        len(climate_zones),
        len(curtains),
        len(ventilation_devices),
        len(doors),
    )

    return {
        "rooms": list(rooms.values()),
        "lights": lights,
        "climate_zones": climate_zones,
        "doors": doors,
        "curtains": curtains,
        "ventilation": ventilation_devices,
        "saunas": [],
        "intercom": {},
    }


def _apply_light_state_update(
    message: dict[str, Any],
    current: dict[str, Any],
    delta: dict[str, Any],
    device_id: int | str,
    param_str: str,
    pending_commands: dict[tuple[int, int], dict[str, Any]] | None,
) -> None:
    """Apply a light state update from a broadcast message.

    Uses pending-command tracking to route SET acknowledgements to the correct
    (room_id, id, sw) entry when id/address alone is ambiguous across rooms.
    Falls back to updating all lights sharing the same id for unsolicited
    broadcasts (physical wall switch presses, other clients).
    """
    # Extract address from the message (may be a single-element list)
    raw_address = message.get("address")
    if isinstance(raw_address, list) and raw_address:
        address_str = str(raw_address[0])
    else:
        address_str = str(raw_address) if raw_address is not None else ""

    lights_copy = [dict(light) for light in current.get("lights", [])]
    found = False

    # Try to resolve ambiguity via the pending-command map
    pending_key: tuple[int, int] | None = None
    if address_str and pending_commands is not None:
        try:
            pending_key = (int(device_id), int(address_str))
        except ValueError, TypeError:
            pending_key = None

    if pending_key is not None and pending_commands is not None:
        pending = pending_commands.get(pending_key)
        if pending is not None:
            # Route to the exact (room_id, id, sw) entry from our pending map
            pending_room_id = pending.get("room_id")
            pending_sw = pending.get("sw")
            for light in lights_copy:
                if (
                    light.get("room_id") == pending_room_id
                    and light.get("id") == device_id
                    and light.get("sw") == pending_sw
                ):
                    light["param"] = param_str
                    found = True
                    break
            # Remove the consumed pending entry
            del pending_commands[pending_key]
            LOGGER.debug(
                "Routed broadcast ack via pending: id=%s address=%s -> room_id=%s sw=%s param=%s",
                device_id,
                address_str,
                pending_room_id,
                pending_sw,
                param_str,
            )

    if not found:
        # No pending entry — unsolicited broadcast. Fall back to updating
        # all lights matching this id (multi-switch within same room).
        # This is ambiguous when id is reused across rooms (e.g. binary
        # relays type "0"), so log a warning.
        for light in lights_copy:
            try:
                light_id = int(light.get("id", 0))
            except ValueError, TypeError:
                light_id = light.get("id")
            if light_id == device_id:
                light["param"] = param_str
                found = True
                # Don't break — update all lights with this id (multi-switch)
        if found:
            LOGGER.warning(
                "Ambiguous light broadcast: id=%s address=%s has no pending "
                "entry — updated ALL lights sharing this id. If this is a "
                "physical wall switch press affecting two rooms, Bisly's "
                "protocol may not distinguish them.",
                device_id,
                address_str,
            )

    if found:
        delta["lights"] = lights_copy


def parse_broadcast_message(
    message: dict[str, Any],
    current: dict[str, Any],
    pending_commands: dict[tuple[int, int], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    """
    Parse a broadcast message into a state delta.

    Broadcast messages from the Bisly server contain device lists with "kind"
    fields. Extract device data and return a normalized delta dict suitable for
    entity platforms to consume.

    Args:
        message: The parsed broadcast JSON message.
        current: The current coordinator data dict.
        pending_commands: Optional map of (id, address) → {room_id, sw, expires_at}
            for pending commands. Used to route SET acks to the correct device
            when id/address alone is ambiguous across rooms.

    Returns:
        A delta dict to merge into coordinator data, or None if not actionable.
    """
    command = message.get("command", "")

    # Server availability signals
    if command == "ping":
        return None  # Keepalive, no state change

    # Single-device state update (must come BEFORE device list check because
    # SET responses can have param as a single-element list like ["1"])
    param = message.get("param")
    raw_device_id = message.get("id")
    if param is not None and raw_device_id is not None:
        # Try to parse param as JSON if string (or list)
        if isinstance(param, str):
            with contextlib.suppress(json.JSONDecodeError):
                param = json.loads(param)
        if isinstance(param, list):
            # Server sends param as array for state changes: ["1"] or ["0"]
            param = str(param[-1]) if param else str(param)
        param_str = str(param)

        # Normalize device_id — server sends int, broadcast sends str
        try:
            device_id = int(raw_device_id)
        except ValueError, TypeError:
            device_id = raw_device_id

        delta: dict[str, Any] = {}

        # Update the device in the lights list so entities see the new state
        if command == "light":
            _apply_light_state_update(message, current, delta, device_id, param_str, pending_commands)

        # Also update climate zones — both floor heating toggles and temperature changes
        if command == "climate":
            climate_copy = [dict(zone) for zone in current.get("climate_zones", [])]
            found = False
            for zone in climate_copy:
                # Floor heating broadcast: id is the room_id, type is "28"
                if str(zone.get("type")) == "28" and zone.get("room_id") == device_id:
                    # air_climate_status: 0=off, 1=on — broadcast sends param=["1"] or ["0"]
                    zone["air_climate_status"] = int(param) if isinstance(param, str) and param.isdigit() else 0
                    found = True
                # Thermostat broadcast: may update temperature or mode
                elif zone.get("room_id") == device_id or zone.get("id") == device_id:
                    if param is not None:
                        zone["param"] = str(param)
                    # If the broadcast includes temp fields, update them
                    if "current_temp" in message:
                        zone["current_temp"] = message["current_temp"]
                    if "target_temp" in message:
                        zone["target_temp"] = message["target_temp"]
                    if "mode" in message:
                        zone["mode"] = message["mode"]
                    found = True
            if found:
                # Propagate air_climate_status from type 28 zones to sibling
                # type 1/2 zones so climate entities see the real running state.
                fh_status: dict[int, Any] = {}
                for zone in climate_copy:
                    if str(zone.get("type")) == "28":
                        fh_status[zone["room_id"]] = zone.get("air_climate_status")
                for zone in climate_copy:
                    if str(zone.get("type")) in ("1", "2") and zone["room_id"] in fh_status:
                        zone["air_climate_status"] = fh_status[zone["room_id"]]

                delta["climate_zones"] = climate_copy

        # Only return delta if it contains actual state changes (beyond metadata).
        # This avoids defeating always_update=False with every broadcast.
        has_state_change = any(k != "_last_broadcast" for k in delta)
        if not has_state_change:
            return None

        delta["_last_broadcast"] = {
            "command": command,
            "type": str(message.get("type", "")),
            "param": param,
            "timestamp": message.get("request_id"),
        }

        return delta

    # Device list broadcast — contains all devices of a given kind
    # (only reached when no id field is present — param is a list of device objects)
    devices = message.get("param")
    if isinstance(devices, list):
        return parse_rooms_response({"param": devices})

    # Unknown broadcast — log and skip
    LOGGER.debug("Unhandled broadcast command=%s keys=%s", command, list(message.keys())[:10])
    return None


def _deep_merge(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge delta into base, returning a new dict."""
    result = {**base}
    for key, value in delta.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result
