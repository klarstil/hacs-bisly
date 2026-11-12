"""Fan platform for hacs_bisly.

Provides fan entities for Bisly ventilation units (ControllerType.VENTILATION = "4").
The app toggles the fan via the SET_FAN sub-command (type "19") with "0"/"1".
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.hacs_bisly.const import DOMAIN, LOGGER, MANUFACTURER, MODEL, VENTILATION_ITEM_SET_FAN
from custom_components.hacs_bisly.entity.base import BislyEntity
from homeassistant.components.fan import FanEntity, FanEntityDescription
from homeassistant.helpers.device_registry import DeviceInfo

if TYPE_CHECKING:
    from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator
    from custom_components.hacs_bisly.data import BislyConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

PARALLEL_UPDATES = 0


class BislyFan(BislyEntity, FanEntity):
    """Fan entity for a Bisly ventilation unit."""

    def __init__(
        self,
        coordinator: BislyDataUpdateCoordinator,
        entity_description: FanEntityDescription,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the fan.

        Args:
            coordinator: The data update coordinator.
            entity_description: The entity description.
            device_data: The full device dict from controller_list (rooms response).
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
        self._attr_name = device_label
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_fan_{room_id}_{device_id}_{device_data.get('sw', 0)}"
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

    @staticmethod
    def _power(device_data: dict[str, Any]) -> str:
        """Extract the fan power string from the device param."""
        param = device_data.get("param")
        if isinstance(param, dict):
            param = param.get("param", param.get("power", "0"))
        return str(param) if param is not None else "0"

    @property
    def is_on(self) -> bool:
        """Return True if the fan is on."""
        return self._power(self._get_live_device_data()) != "0"

    def _update_device_param(self, param: Any) -> None:
        """Optimistically update the fan state for instant UI feedback."""
        self._device_data["param"] = param
        device_room_id = self._device_data.get("room_id")
        device_id = self._device_data.get("id")
        device_sw = self._device_data.get("sw")
        for device in self.coordinator.data.get("ventilation", []):
            if (
                device.get("room_id") == device_room_id
                and device.get("id") == device_id
                and device.get("sw") == device_sw
            ):
                device["param"] = param
                break

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the fan on."""
        self._update_device_param("1")
        await self.coordinator.async_set_ventilation(self._device_data, item_type=VENTILATION_ITEM_SET_FAN, param="1")

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the fan off."""
        self._update_device_param("0")
        await self.coordinator.async_set_ventilation(self._device_data, item_type=VENTILATION_ITEM_SET_FAN, param="0")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BislyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the fan platform from a config entry."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data or {}
    ventilation_data = data.get("ventilation", [])

    entities: list[BislyFan] = []
    for device_data in ventilation_data:
        desc = FanEntityDescription(
            key=f"ventilation_{device_data.get('id')}",
            translation_key="bisly_ventilation",
        )
        entities.append(BislyFan(coordinator, desc, device_data))

    LOGGER.debug("Adding %d Bisly fan entities", len(entities))
    async_add_entities(entities)
