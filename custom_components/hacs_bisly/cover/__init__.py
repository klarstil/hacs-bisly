"""Cover platform for hacs_bisly.

Provides cover entities for Bisly curtains/blinds (ControllerType.WINDOW = "6").
The app treats curtains as binary open/close: param "0" = closed, "1" = open.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.hacs_bisly.const import DOMAIN, LOGGER, MANUFACTURER, MODEL
from custom_components.hacs_bisly.entity.base import BislyEntity
from homeassistant.components.cover import CoverEntity, CoverEntityDescription
from homeassistant.components.cover.const import CoverDeviceClass
from homeassistant.helpers.device_registry import DeviceInfo

if TYPE_CHECKING:
    from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator
    from custom_components.hacs_bisly.data import BislyConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

PARALLEL_UPDATES = 0


class BislyCover(BislyEntity, CoverEntity):
    """Cover entity for a Bisly curtain/blind."""

    _attr_device_class = CoverDeviceClass.CURTAIN

    def __init__(
        self,
        coordinator: BislyDataUpdateCoordinator,
        entity_description: CoverEntityDescription,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the cover.

        Args:
            coordinator: The data update coordinator.
            entity_description: The entity description.
            device_data: The full device dict from controller_list (rooms response).
        """
        super().__init__(coordinator, entity_description)
        self._device_data = device_data
        device_id = device_data.get("id", "unknown")
        room_id = device_data.get("room_id", "")
        device_label = device_data.get("label", f"Curtain {device_id}")

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
            f"{coordinator.config_entry.entry_id}_cover_{room_id}_{device_id}_{device_data.get('sw', 0)}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    f"{coordinator.config_entry.entry_id}_cover_{room_id}_{device_id}_{device_data.get('sw', 0)}",
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
        for curtain in self.coordinator.data.get("curtains", []):
            if (
                curtain.get("room_id") == device_room_id
                and curtain.get("id") == device_id
                and curtain.get("sw") == device_sw
            ):
                return curtain
        return self._device_data

    @staticmethod
    def _position(device_data: dict[str, Any]) -> str:
        """Extract the curtain position string from the device param.

        The raw param may be a plain string ("0"/"1") or a nested dict.
        """
        param = device_data.get("param")
        if isinstance(param, dict):
            param = param.get("param", param.get("position", "0"))
        return str(param) if param is not None else "0"

    @property
    def is_closed(self) -> bool:
        """Return True if the curtain is closed."""
        return self._position(self._get_live_device_data()) == "0"

    def _update_device_param(self, param: Any) -> None:
        """Optimistically update the curtain state for instant UI feedback."""
        self._device_data["param"] = param
        device_room_id = self._device_data.get("room_id")
        device_id = self._device_data.get("id")
        device_sw = self._device_data.get("sw")
        for curtain in self.coordinator.data.get("curtains", []):
            if (
                curtain.get("room_id") == device_room_id
                and curtain.get("id") == device_id
                and curtain.get("sw") == device_sw
            ):
                curtain["param"] = param
                break

    async def async_open_cover(self, **kwargs: Any) -> None:
        """Open the curtain."""
        self._update_device_param("1")
        await self.coordinator.async_set_curtain(self._device_data, "1")

    async def async_close_cover(self, **kwargs: Any) -> None:
        """Close the curtain."""
        self._update_device_param("0")
        await self.coordinator.async_set_curtain(self._device_data, "0")


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BislyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the cover platform from a config entry."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data or {}
    curtains_data = data.get("curtains", [])

    entities: list[BislyCover] = []
    for device_data in curtains_data:
        desc = CoverEntityDescription(
            key=f"curtain_{device_data.get('id')}",
            translation_key="bisly_curtain",
        )
        entities.append(BislyCover(coordinator, desc, device_data))

    LOGGER.debug("Adding %d Bisly cover entities", len(entities))
    async_add_entities(entities)
