"""Lock platform for hacs_bisly.

Provides lock entities for Bisly door intercoms (video door devices)
that support lock/unlock operation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.hacs_bisly.api import BislyApiClientError
from custom_components.hacs_bisly.const import DOMAIN, LOGGER, MANUFACTURER, MODEL
from custom_components.hacs_bisly.entity.base import BislyEntity
from homeassistant.components.lock import LockEntity, LockEntityDescription
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

if TYPE_CHECKING:
    from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator
    from custom_components.hacs_bisly.data import BislyConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

PARALLEL_UPDATES = 0


class BislyDoorLock(BislyEntity, LockEntity):
    """Lock entity for a Bisly door (video door intercom)."""

    _attr_icon = "mdi:door"

    def __init__(
        self,
        coordinator: BislyDataUpdateCoordinator,
        entity_description: LockEntityDescription,
        room_id: int,
        room_label: str,
        door_data: dict[str, Any],
    ) -> None:
        super().__init__(coordinator, entity_description)
        self._room_id = room_id
        self._door_id = door_data.get("id")
        self._door_address = str(door_data.get("address", ""))
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_door_{room_id}_{self._door_id or self._door_address}"
        )
        self._attr_has_entity_name = False
        self._attr_name = f"{room_label} Door"
        self._attr_device_info = DeviceInfo(
            identifiers={
                (DOMAIN, f"{coordinator.config_entry.entry_id}_door_{room_id}_{self._door_id or self._door_address}")
            },
            name=f"{room_label} Door",
            manufacturer=MANUFACTURER,
            model=MODEL,
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )

    def _door(self) -> dict[str, Any]:
        """Return the current door data from the coordinator."""
        for door in self.coordinator.data.get("doors", []):
            if door.get("room_id") != self._room_id:
                continue
            if self._door_id is not None and door.get("id") == self._door_id:
                return door
            if self._door_id is None and str(door.get("address", "")) == self._door_address:
                return door
        return {}

    @property
    def is_locked(self) -> bool | None:
        """Return True if the door is locked."""
        door = self._door()
        if not door:
            return None
        return str(door.get("param", "")) == "0"

    async def async_lock(self, **_: Any) -> None:
        """Lock the door."""
        door = self._door()
        try:
            await self.coordinator.async_set_door(door, state="0")
        except BislyApiClientError as exc:
            LOGGER.exception("Failed to lock door in room %s", self._room_id)
            raise HomeAssistantError(
                translation_domain="hacs_bisly",
                translation_key="door_lock_failed",
            ) from exc

    async def async_unlock(self, **_: Any) -> None:
        """Unlock the door."""
        door = self._door()
        try:
            await self.coordinator.async_set_door(door, state="1")
        except BislyApiClientError as exc:
            LOGGER.exception("Failed to unlock door in room %s", self._room_id)
            raise HomeAssistantError(
                translation_domain="hacs_bisly",
                translation_key="door_unlock_failed",
            ) from exc


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BislyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the lock platform from a config entry."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data or {}
    doors = data.get("doors", [])
    rooms = data.get("rooms", [])

    room_labels: dict[int, str] = {}
    for room in rooms:
        rid = room.get("id")
        if rid is not None:
            room_labels[rid] = room.get("label", f"Room {rid}")

    entities: list[BislyDoorLock] = []
    for door in doors:
        room_id = door.get("room_id")
        if room_id is None:
            continue

        room_label = room_labels.get(room_id, f"Room {room_id}")
        desc = LockEntityDescription(
            key=f"door_{door.get('id')}",
            translation_key="bisly_door",
        )
        entities.append(BislyDoorLock(coordinator, desc, room_id, room_label, door))

    LOGGER.debug("Adding %d Bisly door locks", len(entities))
    async_add_entities(entities)
