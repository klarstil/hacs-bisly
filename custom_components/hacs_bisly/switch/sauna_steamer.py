"""Sauna steamer switch for hacs_bisly."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.hacs_bisly.api import BislyApiClientError
from custom_components.hacs_bisly.const import DOMAIN, LOGGER, MANUFACTURER, MODEL, SAUNA_ITEM_STREAMER
from custom_components.hacs_bisly.entity.base import BislyEntity
from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

if TYPE_CHECKING:
    from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator

PARALLEL_UPDATES = 0


class BislySaunaSteamer(BislyEntity, SwitchEntity):
    """Switch for the sauna steam generator (STREAMER)."""

    _attr_icon = "mdi:steam"

    def __init__(
        self,
        coordinator: BislyDataUpdateCoordinator,
        entity_description: SwitchEntityDescription,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the steamer switch.

        Args:
            coordinator: The data update coordinator.
            entity_description: The entity description.
            device_data: The full sauna device dict from controller_list.
        """
        super().__init__(coordinator, entity_description)
        self._device_data = device_data
        device_id = device_data.get("id", "unknown")
        room_id = device_data.get("room_id", "")
        device_label = device_data.get("label", f"Sauna {device_id}")

        room_label = ""
        for room in coordinator.data.get("rooms", []):
            if room.get("id") == room_id:
                room_label = room.get("label", "")
                break
        if room_label:
            device_label = f"{room_label} - {device_label}"

        self._attr_has_entity_name = False
        self._attr_name = f"{device_label} Steamer"
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_sauna_steamer_{room_id}_{device_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{coordinator.config_entry.entry_id}_sauna_{room_id}_{device_id}")},
            name=device_label,
            manufacturer=MANUFACTURER,
            model=MODEL,
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )

    def _get_live_device_data(self) -> dict[str, Any]:
        """Look up the current device data from the coordinator, falling back to setup-time data."""
        device_room_id = self._device_data.get("room_id")
        device_id = self._device_data.get("id")
        for sauna in self.coordinator.data.get("saunas", []):
            if sauna.get("room_id") == device_room_id and sauna.get("id") == device_id:
                return sauna
        return self._device_data

    @property
    def is_on(self) -> bool:
        """Return True if the steamer is on."""
        return str(self._get_live_device_data().get("steam", "0")) == "1"

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the steamer on."""
        try:
            await self.coordinator.async_set_sauna(self._device_data, param="1", item_type=SAUNA_ITEM_STREAMER)
        except BislyApiClientError as exc:
            LOGGER.exception("Failed to turn on sauna steamer")
            raise HomeAssistantError(
                translation_domain="hacs_bisly",
                translation_key="sauna_steamer_turn_on_failed",
            ) from exc

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the steamer off."""
        try:
            await self.coordinator.async_set_sauna(self._device_data, param="0", item_type=SAUNA_ITEM_STREAMER)
        except BislyApiClientError as exc:
            LOGGER.exception("Failed to turn off sauna steamer")
            raise HomeAssistantError(
                translation_domain="hacs_bisly",
                translation_key="sauna_steamer_turn_off_failed",
            ) from exc


def build_sauna_steamer_entities(coordinator: BislyDataUpdateCoordinator) -> list[BislySaunaSteamer]:
    """Build sauna steamer switch entities for the given coordinator."""
    data = coordinator.data or {}
    entities: list[BislySaunaSteamer] = []
    for device_data in data.get("saunas", []):
        desc = SwitchEntityDescription(
            key=f"sauna_steamer_{device_data.get('id')}",
            translation_key="sauna_steamer",
        )
        entities.append(BislySaunaSteamer(coordinator, desc, device_data))
    return entities
