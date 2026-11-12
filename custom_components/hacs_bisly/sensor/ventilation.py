"""Ventilation sensors for hacs_bisly.

Provides CO2 and humidity sensors for Bisly ventilation units.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.hacs_bisly.const import DOMAIN, MANUFACTURER, MODEL
from custom_components.hacs_bisly.entity.base import BislyEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorEntityDescription, SensorStateClass
from homeassistant.const import CONCENTRATION_PARTS_PER_MILLION, PERCENTAGE
from homeassistant.helpers.device_registry import DeviceInfo

if TYPE_CHECKING:
    from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator

PARALLEL_UPDATES = 0

VENTILATION_SENSOR_TYPES = [
    SensorEntityDescription(
        key="co2",
        translation_key="co2",
        device_class=SensorDeviceClass.CO2,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=CONCENTRATION_PARTS_PER_MILLION,
        suggested_display_precision=0,
        name="CO2",
    ),
    SensorEntityDescription(
        key="humidity",
        translation_key="humidity",
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=PERCENTAGE,
        suggested_display_precision=1,
        name="Humidity",
    ),
]

_VENTILATION_FIELD_MAP = {
    "co2": "current_co2",
    "humidity": "current_humidity",
}


class BislyVentilationSensor(BislyEntity, SensorEntity):
    """Sensor for a Bisly ventilation CO2/humidity reading."""

    def __init__(
        self,
        coordinator: BislyDataUpdateCoordinator,
        entity_description: SensorEntityDescription,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the sensor.

        Args:
            coordinator: The data update coordinator.
            entity_description: The entity description.
            device_data: The full ventilation device dict from controller_list.
        """
        super().__init__(coordinator, entity_description)
        self._device_data = device_data
        device_id = device_data.get("id", "unknown")
        room_id = device_data.get("room_id", "")
        device_label = device_data.get("label", f"Ventilation {device_id}")

        room_label = ""
        for room in coordinator.data.get("rooms", []):
            if room.get("id") == room_id:
                room_label = room.get("label", "")
                break
        if room_label:
            device_label = f"{room_label} - {device_label}"

        self._attr_has_entity_name = False
        self._attr_name = f"{device_label} {entity_description.name}"
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_ventilation_{room_id}_{device_id}_"
            f"{device_data.get('sw', 0)}_{entity_description.key}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    f"{coordinator.config_entry.entry_id}_fan_{room_id}_{device_id}_{device_data.get('sw', 0)}",
                )
            },
            name=device_label,
            manufacturer=MANUFACTURER,
            model=MODEL,
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )

    def _get_live_device_data(self) -> dict[str, Any]:
        """Look up the current device data from the coordinator, falling back to setup-time data."""
        device_room_id = self._device_data.get("room_id")
        device_id = self._device_data.get("id")
        device_sw = self._device_data.get("sw")
        for device in self.coordinator.data.get("ventilation", []):
            if (
                device.get("room_id") == device_room_id
                and device.get("id") == device_id
                and device.get("sw") == device_sw
            ):
                return device
        return self._device_data

    @property
    def native_value(self) -> float | None:
        """Return the sensor value from the ventilation device data."""
        field = _VENTILATION_FIELD_MAP.get(str(self.entity_description.key))
        if field is None:
            return None
        val = self._get_live_device_data().get(field)
        if val is not None:
            try:
                return float(val)
            except ValueError, TypeError:
                pass
        return None
