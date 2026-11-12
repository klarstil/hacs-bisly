"""Sauna climate entity for hacs_bisly."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.hacs_bisly.api import BislyApiClientError
from custom_components.hacs_bisly.const import (
    DOMAIN,
    LOGGER,
    MANUFACTURER,
    MODEL,
    SAUNA_ITEM_HEATING,
    SAUNA_ITEM_TEMPERATURE,
    SAUNA_MAX_TEMPERATURE,
    SAUNA_MIN_TEMPERATURE,
)
from custom_components.hacs_bisly.entity.base import BislyEntity
from homeassistant.components.climate import ClimateEntity, ClimateEntityDescription
from homeassistant.components.climate.const import ClimateEntityFeature, HVACMode
from homeassistant.const import UnitOfTemperature
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

if TYPE_CHECKING:
    from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator

PARALLEL_UPDATES = 0


class BislySauna(BislyEntity, ClimateEntity):
    """Climate entity for a Bisly sauna."""

    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT]
    _attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE
    _attr_min_temp = SAUNA_MIN_TEMPERATURE
    _attr_max_temp = SAUNA_MAX_TEMPERATURE
    _attr_target_temperature_step = 1.0

    def __init__(
        self,
        coordinator: BislyDataUpdateCoordinator,
        entity_description: ClimateEntityDescription,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the sauna climate entity.

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
        self._attr_name = device_label
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_sauna_{room_id}_{device_id}"
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
    def current_temperature(self) -> float | None:
        """Return the current sauna temperature."""
        val = self._get_live_device_data().get("current_temp")
        if val is not None:
            try:
                return float(val)
            except ValueError, TypeError:
                pass
        return None

    @property
    def target_temperature(self) -> float | None:
        """Return the target sauna temperature."""
        val = self._get_live_device_data().get("target_temp")
        if val is not None:
            try:
                return float(val)
            except ValueError, TypeError:
                pass
        return None

    @property
    def hvac_mode(self) -> HVACMode:
        """Return the current HVAC mode.

        The sauna's on/off state is the app's "manual" flag (1 = on).
        """
        manual = self._get_live_device_data().get("manual")
        return HVACMode.HEAT if str(manual) == "1" else HVACMode.OFF

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set the target temperature."""
        temp = kwargs.get("temperature")
        if temp is None:
            return
        try:
            await self.coordinator.async_set_sauna(
                self._device_data, param=str(int(temp)), item_type=SAUNA_ITEM_TEMPERATURE
            )
        except BislyApiClientError as exc:
            LOGGER.exception("Failed to set sauna temperature")
            raise HomeAssistantError(
                translation_domain="hacs_bisly",
                translation_key="sauna_set_temperature_failed",
            ) from exc

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Set the HVAC mode (on/off)."""
        param = "1" if hvac_mode == HVACMode.HEAT else "0"
        try:
            await self.coordinator.async_set_sauna(self._device_data, param=param, item_type=SAUNA_ITEM_HEATING)
        except BislyApiClientError as exc:
            LOGGER.exception("Failed to set sauna mode")
            raise HomeAssistantError(
                translation_domain="hacs_bisly",
                translation_key="sauna_set_mode_failed",
            ) from exc


def build_sauna_entities(coordinator: BislyDataUpdateCoordinator) -> list[BislySauna]:
    """Build sauna climate entities for the given coordinator."""
    data = coordinator.data or {}
    entities: list[BislySauna] = []
    for device_data in data.get("saunas", []):
        desc = ClimateEntityDescription(
            key=f"sauna_{device_data.get('id')}",
            translation_key="bisly_sauna",
        )
        entities.append(BislySauna(coordinator, desc, device_data))
    return entities
