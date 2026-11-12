"""Climate platform for hacs_bisly.

Provides climate entities for rooms with temperature control (heating).
Each room with a thermostat zone gets a climate entity.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.hacs_bisly.api import BislyApiClientError
from custom_components.hacs_bisly.climate.sauna import build_sauna_entities
from custom_components.hacs_bisly.const import (
    AIR_CLIMATE_AUTO,
    AIR_CLIMATE_COOLING,
    AIR_CLIMATE_HEATING,
    AIR_CLIMATE_OFF,
    DOMAIN,
    LOGGER,
    MANUFACTURER,
    MODEL,
)
from custom_components.hacs_bisly.entity.base import BislyEntity
from homeassistant.components.climate import ClimateEntity, ClimateEntityDescription
from homeassistant.components.climate.const import ClimateEntityFeature, HVACAction, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

if TYPE_CHECKING:
    from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator
    from custom_components.hacs_bisly.data import BislyConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

PARALLEL_UPDATES = 0


class BislyClimate(BislyEntity, ClimateEntity):
    """Climate entity for a Bisly room thermostat zone."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
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
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_climate_{room_id}")},
            name=f"{room_label} Climate",
            manufacturer=MANUFACTURER,
            model=MODEL,
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )

    def _zone(self) -> dict[str, Any]:
        """Return the current climate zone data from the coordinator."""
        for zone in self.coordinator.data.get("climate_zones", []):
            if zone.get("room_id") == self._room_id and zone.get("type") in ("1", "2"):
                return zone
        return {}

    def _is_ac_zone(self) -> bool:
        """Return True if this room's thermostat zone is an A/C unit (type 1)."""
        return str(self._zone().get("type")) == "1"

    @property
    def hvac_modes(self) -> list[HVACMode]:
        """Return the supported HVAC modes based on the zone type."""
        if self._is_ac_zone():
            return [HVACMode.OFF, HVACMode.COOL, HVACMode.HEAT, HVACMode.AUTO]
        return [HVACMode.OFF, HVACMode.HEAT]

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

        A/C units (type 1) use air_climate_status (0=off, 1=cooling, 2=heating,
        3=auto). Floor-heating-only rooms always report air_climate_status 0, so
        they fall back to the thermostat "mode" flag plus floor_heating_active.
        """
        zone = self._zone()
        if self._is_ac_zone():
            status = self._air_climate_status(zone)
            if status == AIR_CLIMATE_COOLING:
                return HVACMode.COOL
            if status == AIR_CLIMATE_HEATING:
                return HVACMode.HEAT
            if status == AIR_CLIMATE_AUTO:
                return HVACMode.AUTO
            return HVACMode.OFF

        mode = zone.get("mode")
        if mode != "1":
            return HVACMode.OFF
        floor_active = zone.get("floor_heating_active")
        if floor_active is None or floor_active == 1:
            return HVACMode.HEAT
        return HVACMode.OFF

    @staticmethod
    def _air_climate_status(zone: dict[str, Any]) -> int:
        """Return the air_climate_status as an int (0 when absent/non-numeric)."""
        raw = zone.get("air_climate_status")
        if raw is None:
            return AIR_CLIMATE_OFF
        text = str(raw)
        if not text.isdigit():
            return AIR_CLIMATE_OFF
        return int(text)

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return the current running action (heating/cooling/idle)."""
        zone = self._zone()
        if self._is_ac_zone():
            status = self._air_climate_status(zone)
            if status == AIR_CLIMATE_COOLING:
                return HVACAction.COOLING
            if status == AIR_CLIMATE_HEATING:
                return HVACAction.HEATING
            if status == AIR_CLIMATE_AUTO:
                return None
            return HVACAction.OFF
        if zone.get("floor_heating_active") == 1:
            return HVACAction.HEATING
        return HVACAction.OFF

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
        """Set the HVAC mode.

        A/C units use the air_climate_status scale (0=off, 1=cooling, 2=heating,
        3=auto); floor heating uses the thermostat "activated" flag (0/1).
        """
        zone = self._zone()
        if self._is_ac_zone():
            mode_map = {
                HVACMode.OFF: str(AIR_CLIMATE_OFF),
                HVACMode.COOL: str(AIR_CLIMATE_COOLING),
                HVACMode.HEAT: str(AIR_CLIMATE_HEATING),
                HVACMode.AUTO: str(AIR_CLIMATE_AUTO),
            }
        else:
            mode_map = {
                HVACMode.OFF: "0",
                HVACMode.HEAT: "1",
            }
        mode_value = mode_map.get(hvac_mode, "0")
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

    entities: list[ClimateEntity] = []
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

    # Sauna entities
    entities.extend(build_sauna_entities(coordinator))

    LOGGER.debug("Adding %d Bisly climate entities", len(entities))
    async_add_entities(entities)
