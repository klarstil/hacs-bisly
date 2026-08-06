"""Sensor platform for hacs_bisly.

Provides sensors for room climate data (temperature, humidity, CO2, etc.)
extracted from the coordinator's room data.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.hacs_bisly.const import LOGGER
from custom_components.hacs_bisly.entity.base import BislyEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import CONCENTRATION_PARTS_PER_MILLION, PERCENTAGE, UnitOfTemperature

if TYPE_CHECKING:
    from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator
    from custom_components.hacs_bisly.data import BislyConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

PARALLEL_UPDATES = 0

_SENSOR_TYPES = [
    SensorEntityDescription(
        key="temperature",
        translation_key="temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        suggested_display_precision=1,
        name="Temperature",
    ),
]

# Mapping from our sensor key to possible climate zone fields
_CLIMATE_FIELD_MAP: dict[str, list[str]] = {
    "temperature": ["current_temp", "actual_temp", "temp"],
    "humidity": ["humidity", "rh"],
    "co2": ["co2", "co2_level"],
}


class BislySensor(BislyEntity, SensorEntity):
    """Sensor entity for Bisly room climate data."""

    def __init__(
        self,
        coordinator: BislyDataUpdateCoordinator,
        entity_description: SensorEntityDescription,
        room_id: int,
        room_name: str,
    ) -> None:
        """Initialize the sensor.

        Args:
            coordinator: The data update coordinator.
            entity_description: The entity description.
            room_id: The room ID this sensor is associated with.
            room_name: The room name for display.
        """
        super().__init__(coordinator, entity_description)
        self._room_id = room_id
        self._room_name = room_name
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_sensor_{room_id}_{entity_description.key}"
        self._attr_has_entity_name = False
        self._attr_name = f"{room_name} {self.entity_description.name}"

    @property
    def native_value(self) -> float | None:
        """Return the sensor value from coordinator data."""
        climate_zones = self.coordinator.data.get("climate_zones", [])
        for zone in climate_zones:
            if zone.get("room_id") != self._room_id:
                continue
            # Try the field names specific to this sensor type
            sensor_key = str(self.entity_description.key)
            if sensor_key in _CLIMATE_FIELD_MAP:
                for field in _CLIMATE_FIELD_MAP[sensor_key]:
                    val = zone.get(field)
                    if val is not None:
                        try:
                            return float(val)
                        except ValueError, TypeError:
                            pass
                # Fallback: check generic sensor field
                val = zone.get("sensor")
                if val is not None:
                    try:
                        return float(val)
                    except ValueError, TypeError:
                        pass
            # Direct field lookup
            val = zone.get(sensor_key)
            if val is not None:
                try:
                    return float(val)
                except ValueError, TypeError:
                    pass
            return None
        return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BislyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the sensor platform from a config entry."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data or {}

    rooms = data.get("rooms", [])
    climate_zones = data.get("climate_zones", [])
    # Only create sensors for rooms that actually have climate data
    rooms_with_climate = {zone.get("room_id") for zone in climate_zones if zone.get("room_id") is not None}
    entities: list[BislySensor] = []

    for room in rooms:
        room_id = room.get("id")
        room_name = room.get("label", f"Room {room_id}")
        if room_id is None or room_id not in rooms_with_climate:
            continue

        entities.extend(BislySensor(coordinator, desc, room_id, room_name) for desc in _SENSOR_TYPES)

    LOGGER.debug("Adding %d Bisly sensor entities", len(entities))
    async_add_entities(entities)
