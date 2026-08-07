"""Switch platform for hacs_bisly.

Provides switches for floor heating toggles in rooms that support it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.hacs_bisly.api import BislyApiClientError
from custom_components.hacs_bisly.const import DOMAIN, LOGGER, MANUFACTURER, MODEL
from custom_components.hacs_bisly.entity.base import BislyEntity
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

if TYPE_CHECKING:
    from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator
    from custom_components.hacs_bisly.data import BislyConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

PARALLEL_UPDATES = 0


class BislyFloorHeatingSwitch(BislyEntity, SwitchEntity):
    """Switch entity for Bisly floor heating control."""

    _attr_icon = "mdi:radiator"

    def __init__(
        self,
        coordinator: BislyDataUpdateCoordinator,
        entity_description: SwitchEntityDescription,
        room_id: int,
        room_label: str,
        zone_data: dict[str, Any],
    ) -> None:
        """Initialize the floor heating switch.

        Args:
            coordinator: The data update coordinator.
            entity_description: The entity description.
            room_id: The room ID.
            room_label: Display label for the room.
            zone_data: Full climate zone dict from coordinator data.
        """
        super().__init__(coordinator, entity_description)
        self._room_id = room_id
        self._room_label = room_label
        self._zone_data_ref = zone_data
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_floor_heating_{room_id}"
        self._attr_has_entity_name = False
        self._attr_name = f"{room_label} Floor Heating"
        # Attach to the room's climate device (shared with thermostat + sensors)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_climate_{room_id}")},
            name=f"{room_label} Climate",
            manufacturer=MANUFACTURER,
            model=MODEL,
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )

    def _zone(self) -> dict[str, Any]:
        """Return the current floor heating zone data from the coordinator."""
        for zone in self.coordinator.data.get("climate_zones", []):
            if zone.get("room_id") == self._room_id and str(zone.get("type")) == "28":
                return zone
        return {}

    @property
    def is_on(self) -> bool | None:
        """Return True if floor heating is active."""
        zone = self._zone()
        if not zone:
            return None
        # air_climate_status is the actual running state:
        # 0 = off/offline, 1 = on (heating/cooling active)
        return zone.get("air_climate_status") == 1

    async def async_turn_on(self, **_: Any) -> None:
        """Turn floor heating on."""
        zone = self._zone()
        try:
            await self.coordinator.async_set_climate(zone, mode="1")
        except BislyApiClientError as exc:
            LOGGER.exception("Failed to turn on floor heating for room %s", self._room_id)
            raise HomeAssistantError(
                translation_domain="hacs_bisly",
                translation_key="floor_heating_turn_on_failed",
            ) from exc

    async def async_turn_off(self, **_: Any) -> None:
        """Turn floor heating off."""
        zone = self._zone()
        try:
            await self.coordinator.async_set_climate(zone, mode="0")
        except BislyApiClientError as exc:
            LOGGER.exception("Failed to turn off floor heating for room %s", self._room_id)
            raise HomeAssistantError(
                translation_domain="hacs_bisly",
                translation_key="floor_heating_turn_off_failed",
            ) from exc


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BislyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the switch platform from a config entry."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data or {}
    climate_zones = data.get("climate_zones", [])
    rooms = data.get("rooms", [])

    # Build room lookup
    room_labels: dict[int, str] = {}
    for room in rooms:
        rid = room.get("id")
        if rid is not None:
            room_labels[rid] = room.get("label", f"Room {rid}")

    entities: list[BislyFloorHeatingSwitch] = []
    for zone in climate_zones:
        zone_type = str(zone.get("type", ""))
        room_id = zone.get("room_id")
        # Only create for floor heating zones (type 28)
        if zone_type != "28":
            continue
        if room_id is None:
            continue

        room_label = room_labels.get(room_id, f"Room {room_id}")
        desc = SwitchEntityDescription(
            key=f"floor_heating_{room_id}",
            translation_key="floor_heating",
        )
        entities.append(BislyFloorHeatingSwitch(coordinator, desc, room_id, room_label, zone))

    LOGGER.debug("Adding %d Bisly floor heating switches", len(entities))
    async_add_entities(entities)
