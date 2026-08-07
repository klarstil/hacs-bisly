"""Light platform for hacs_bisly.

Provides light entities for Bisly lighting devices.
Supports on/off, dimming, and RGB control.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.hacs_bisly.const import (
    DOMAIN,
    LIGHTING_RGB_TYPES,
    LIGHTING_SLIDER_TYPES,
    LOGGER,
    MANUFACTURER,
    MODEL,
)
from custom_components.hacs_bisly.entity.base import BislyEntity
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
    LightEntityDescription,
)
from homeassistant.helpers.device_registry import DeviceInfo

if TYPE_CHECKING:
    from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator
    from custom_components.hacs_bisly.data import BislyConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

PARALLEL_UPDATES = 0


class BislyLight(BislyEntity, LightEntity):
    """Light entity for Bisly lighting devices."""

    def __init__(
        self,
        coordinator: BislyDataUpdateCoordinator,
        entity_description: LightEntityDescription,
        device_data: dict[str, Any],
    ) -> None:
        """Initialize the light.

        Args:
            coordinator: The data update coordinator.
            entity_description: The entity description.
            device_data: The full device dict from controller_list (rooms response).
        """
        super().__init__(coordinator, entity_description)
        self._device_data = device_data
        device_id = device_data.get("id", "unknown")
        room_id = device_data.get("room_id", "")
        device_label = device_data.get("label", f"Light {device_id}")

        # Look up room label from coordinator data
        rooms = coordinator.data.get("rooms", [])
        room_label = ""
        for room in rooms:
            if room.get("id") == room_id:
                room_label = room.get("label", "")
                break
        if room_label:
            device_label = f"{room_label} - {device_label}"

        self._attr_has_entity_name = False
        self._attr_name = device_label
        self._attr_unique_id = (
            f"{coordinator.config_entry.entry_id}_light_{room_id}_{device_id}_{device_data.get('sw', 0)}"
        )
        self._attr_device_info = DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    f"{coordinator.config_entry.entry_id}_light_{room_id}_{device_id}_{device_data.get('sw', 0)}",
                )
            },
            name=device_label,
            manufacturer=MANUFACTURER,
            model=MODEL,
            via_device=(DOMAIN, coordinator.config_entry.entry_id),
        )

        # Determine color mode from device type
        device_type = str(device_data.get("type", ""))
        if device_type in LIGHTING_RGB_TYPES:
            self._attr_supported_color_modes = {ColorMode.RGB, ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.RGB
        elif device_type in LIGHTING_SLIDER_TYPES:
            self._attr_supported_color_modes = {ColorMode.BRIGHTNESS}
            self._attr_color_mode = ColorMode.BRIGHTNESS
        else:
            self._attr_supported_color_modes = {ColorMode.ONOFF}
            self._attr_color_mode = ColorMode.ONOFF

    @property
    def is_on(self) -> bool:
        """Return True if the light is on."""
        device_data = self._get_live_device_data()
        return str(device_data.get("param", "0")) != "0"

    @property
    def brightness(self) -> int | None:
        """Return the brightness (0-255) if the light supports dimming."""
        if ColorMode.BRIGHTNESS not in self._attr_supported_color_modes:
            return None
        device_data = self._get_live_device_data()
        param = device_data.get("param", "0")
        try:
            pct = float(param) / 100.0  # Bisly uses 0-100 for dimming
            return max(0, min(255, int(pct * 255)))
        except ValueError, TypeError:
            return None

    @property
    def rgb_color(self) -> tuple[int, int, int] | None:
        """Return the RGB color value."""
        device_data = self._get_live_device_data()
        param = device_data.get("param")
        if isinstance(param, dict):
            r = param.get("r") or param.get("red", 0)
            g = param.get("g") or param.get("green", 0)
            b = param.get("b") or param.get("blue", 0)
            return (int(r), int(g), int(b))
        return None

    def _get_live_device_data(self) -> dict[str, Any]:
        """Look up the current device data from the coordinator, falling back to setup-time data."""
        device_room_id = self._device_data.get("room_id")
        device_id = self._device_data.get("id")
        device_sw = self._device_data.get("sw")
        lights = self.coordinator.data.get("lights", [])
        for light in lights:
            if light.get("room_id") == device_room_id and light.get("id") == device_id and light.get("sw") == device_sw:
                return light
        return self._device_data

    def _update_device_param(self, param: Any) -> None:
        """Optimistically update the light state in coordinator data for instant UI feedback."""
        self._device_data["param"] = param
        device_room_id = self._device_data.get("room_id")
        device_id = self._device_data.get("id")
        device_sw = self._device_data.get("sw")
        lights = self.coordinator.data.get("lights", [])
        for light in lights:
            if light.get("room_id") == device_room_id and light.get("id") == device_id and light.get("sw") == device_sw:
                light["param"] = param
                break

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the light on."""
        if ATTR_BRIGHTNESS in kwargs:
            brightness = kwargs[ATTR_BRIGHTNESS]
            value = max(0, min(100, int(brightness / 255 * 100)))
            self._update_device_param(str(value))
            await self.coordinator.async_set_brightness(self._device_data, value)
            return

        if ATTR_RGB_COLOR in kwargs and ColorMode.RGB in self._attr_supported_color_modes:
            r, g, b = kwargs[ATTR_RGB_COLOR]
            self._update_device_param({"r": r, "g": g, "b": b})
            await self.coordinator.async_set_rgb(self._device_data, r, g, b)
            return

        self._update_device_param("1")
        await self.coordinator.async_set_light(self._device_data, on=True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the light off."""
        self._update_device_param("0")
        await self.coordinator.async_set_light(self._device_data, on=False)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BislyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the light platform from a config entry."""
    coordinator = entry.runtime_data.coordinator
    data = coordinator.data or {}
    lights_data = data.get("lights", [])

    entities: list[BislyLight] = []
    for device_data in lights_data:
        desc = LightEntityDescription(
            key=f"light_{device_data.get('id')}",
            translation_key="bisly_light",
        )

        entities.append(BislyLight(coordinator, desc, device_data))

    LOGGER.debug("Adding %d Bisly light entities", len(entities))
    async_add_entities(entities)
