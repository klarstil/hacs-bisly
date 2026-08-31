"""Button platform for hacs_bisly.

Provides intercom control buttons:
- Hang up the active intercom call
- Open the door linked to an active call (one entity per door action ID)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.hacs_bisly.api import BislyApiClientError
from custom_components.hacs_bisly.const import DOMAIN, LOGGER, MANUFACTURER, MODEL
from custom_components.hacs_bisly.entity.base import BislyEntity
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

if TYPE_CHECKING:
    from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator
    from custom_components.hacs_bisly.data import BislyConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

PARALLEL_UPDATES = 0

# Door open buttons are (re)created for these action IDs across calls.
# Keyed by (entry_id, action_id) so multiple config entries don't collide.
_OPEN_DOOR_ENTITIES: dict[tuple[str, str], BislyOpenDoorButton] = {}


class BislyHangupButton(BislyEntity, ButtonEntity):
    """Button that hangs up the active intercom call."""

    _attr_icon = "mdi:phone-hangup"

    @property
    def available(self) -> bool:
        """Only available while a call is active."""
        intercom = (self.coordinator.data or {}).get("intercom", {})
        return bool(intercom.get("call_id"))

    async def async_press(self) -> None:
        """Hang up the active call."""
        try:
            await self.coordinator.async_hangup_intercom()
        except BislyApiClientError as exc:
            LOGGER.exception("Failed to hang up intercom call")
            raise HomeAssistantError(
                translation_domain="hacs_bisly",
                translation_key="intercom_hangup_failed",
            ) from exc


class BislyOpenDoorButton(BislyEntity, ButtonEntity):
    """Button that opens a door linked to the active intercom call."""

    _attr_icon = "mdi:door-open"

    def __init__(
        self,
        coordinator: BislyDataUpdateCoordinator,
        entity_description: ButtonEntityDescription,
        action_id: str,
    ) -> None:
        """Initialize the button."""
        super().__init__(coordinator, entity_description)
        self._action_id = action_id
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_door_open_{action_id}"
        self._attr_has_entity_name = False
        self._attr_name = f"Door Open {action_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name=coordinator.config_entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    @property
    def available(self) -> bool:
        """Only available when a call with a door link is active."""
        intercom = (self.coordinator.data or {}).get("intercom", {})
        return self._action_id in intercom.get("doors", [])

    async def async_press(self) -> None:
        """Execute the door-open action."""
        try:
            await self.coordinator.async_open_door_intercom(self._action_id)
        except BislyApiClientError as exc:
            LOGGER.exception("Failed to open door (action_id=%s)", self._action_id)
            raise HomeAssistantError(
                translation_domain="hacs_bisly",
                translation_key="door_open_failed",
            ) from exc


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BislyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the button platform from a config entry."""
    coordinator = entry.runtime_data.coordinator

    entities: list[ButtonEntity] = [
        BislyHangupButton(
            coordinator,
            ButtonEntityDescription(key="intercom_hangup", translation_key="intercom_hangup"),
        )
    ]
    async_add_entities(entities)

    # Door-open buttons appear once the first call delivers door IDs
    def _add_door_buttons() -> None:
        intercom = (coordinator.data or {}).get("intercom", {})
        door_ids = intercom.get("doors", [])
        new_entities: list[BislyOpenDoorButton] = []
        for action_id in door_ids:
            key = (coordinator.config_entry.entry_id, action_id)
            if key not in _OPEN_DOOR_ENTITIES:
                button = BislyOpenDoorButton(
                    coordinator,
                    ButtonEntityDescription(
                        key=f"door_open_{action_id}",
                        translation_key="door_open",
                    ),
                    action_id,
                )
                _OPEN_DOOR_ENTITIES[key] = button
                new_entities.append(button)
        if new_entities:
            async_add_entities(new_entities)
            LOGGER.debug("Added %d door-open buttons", len(new_entities))

    coordinator.async_add_listener(_add_door_buttons)
