"""Climate platform for hacs_bisly.

Provides climate entities for rooms with temperature control (heating).
Each room with a thermostat zone gets a climate entity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.hacs_bisly.api import BislyApiClientError
from custom_components.hacs_bisly.const import LOGGER
from custom_components.hacs_bisly.entity.base import BislyEntity
from homeassistant.components.climate import ClimateEntity, ClimateEntityDescription, ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError

if TYPE_CHECKING:
    from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator
    from custom_components.hacs_bisly.data import BislyConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

PARALLEL_UPDATES = 0


class BislyClimate(BislyEntity, ClimateEntity):
    """Climate entity for a Bisly room thermostat zone."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE

    def __init__(
        self,
        coordinator: BislyDataUpdateCoordinator,
        entity_description: ClimateEntityDescription,
        room_id: int,
        room_label: str,
        zone_data: dict[str, Any],
    ) -> None:
        """Initialize the climate entity.

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
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_climate_{room_id}"
        self._attr_has_entity_name = False
        self._attr_name = f"{room_label} Climate"

    def _zone(self) -> dict[str, Any]:
        """Return the current climate zone data from the coordinator."""
        for zone in self.coordinator.data.get("climate_zones", []):
            if zone.get("room_id") == self._room_id and zone.get("type") in ("1", "2"):
                return zone
        return {}

    @property
    def current_temperature(self) -> float | None:
        """Return the current room temperature."""
        val = self._zone().get("current_temp")
        if val is not None:
            try:
                return float(val)
            except ValueError, TypeError:
                pass
        return None

    @property
    def target_temperature(self) -> float | None:
        """Return the target temperature."""
        val = self._zone().get("target_temp")
        if val is not None:
            try:
                return float(val)
            except ValueError, TypeError:
                pass
        return None

    @property
    def min_temp(self) -> float:
        """Return the minimum temperature."""
        val = self._zone().get("min_temp")
        if val is not None:
            try:
                return float(val)
            except ValueError, TypeError:
                pass
        return 5.0

    @property
    def max_temp(self) -> float:
        """Return the maximum temperature."""
        val = self._zone().get("max_temp")
        if val is not None:
            try:
                return float(val)
            except ValueError, TypeError:
                pass
        return 35.0

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode.

        The thermostat's "activated" field is the configured mode (0/1), but the
        actual running state is air_climate_status from the sibling floor heating
        (type 28) zone.  When the floor heating is offline, air_climate_status is 0
        even though activated may still be "1".  Show OFF in that case.
        When no floor heating sibling exists (air_climate_status absent), fall
        back to the thermostat's own mode field.
        """
        zone = self._zone()
        mode = zone.get("mode")
        if mode != "1":
            return HVACMode.OFF
        air_status = zone.get("air_climate_status")
        if air_status is None:
            # No floor heating sibling — trust the thermostat's mode
            return HVACMode.HEAT
        if air_status == 1:
            return HVACMode.HEAT
        return HVACMode.OFF

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature."""
        temp = kwargs.get("temperature")
        if temp is None:
            return
        zone = self._zone()
        try:
            await self.coordinator.async_set_climate(zone, temp=float(temp))
        except BislyApiClientError as exc:
            LOGGER.exception("Failed to set climate temperature for room %s", self._room_id)
            raise HomeAssistantError(
                translation_domain="hacs_bisly",
                translation_key="climate_set_temperature_failed",
            ) from exc

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode."""
        mode_value = "1" if hvac_mode == HVACMode.HEAT else "0"
        zone = self._zone()
        try:
            await self.coordinator.async_set_climate(zone, mode=mode_value)
        except BislyApiClientError as exc:
            LOGGER.exception("Failed to set climate mode for room %s", self._room_id)
            raise HomeAssistantError(
                translation_domain="hacs_bisly",
                translation_key="climate_set_mode_failed",
            ) from exc


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BislyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the climate platform from a config entry."""
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

    entities: list[BislyClimate] = []
    for zone in climate_zones:
        zone_type = str(zone.get("type", ""))
        room_id = zone.get("room_id")
        # Only create for thermostat zones (type 1 or 2), not floor heating (type 28)
        if zone_type not in ("1", "2"):
            continue
        if room_id is None:
            continue
        # Only create if both current and target temp exist
        if zone.get("current_temp") is None and zone.get("target_temp") is None:
            continue

        room_label = room_labels.get(room_id, f"Room {room_id}")
        desc = ClimateEntityDescription(
            key=f"climate_{room_id}",
            translation_key="bisly_climate",
        )
        entities.append(BislyClimate(coordinator, desc, room_id, room_label, zone))

    LOGGER.debug("Adding %d Bisly climate entities", len(entities))
    async_add_entities(entities)
