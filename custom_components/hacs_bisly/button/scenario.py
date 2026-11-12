"""Scenario button for hacs_bisly."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from custom_components.hacs_bisly.api import BislyApiClientError
from custom_components.hacs_bisly.const import DOMAIN, LOGGER, MANUFACTURER, MODEL
from custom_components.hacs_bisly.entity.base import BislyEntity
from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo

if TYPE_CHECKING:
    from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator

PARALLEL_UPDATES = 0


class BislyScenarioButton(BislyEntity, ButtonEntity):
    """Button that executes a Bisly scenario/quick action."""

    _attr_icon = "mdi:play-circle"

    def __init__(
        self,
        coordinator: BislyDataUpdateCoordinator,
        entity_description: ButtonEntityDescription,
        action_data: dict[str, Any],
    ) -> None:
        """Initialize the scenario button.

        Args:
            coordinator: The data update coordinator.
            entity_description: The entity description.
            action_data: The action dict from the controller_list type-15 response.
        """
        super().__init__(coordinator, entity_description)
        self._action_id = str(action_data.get("id", ""))
        label = action_data.get("label", f"Action {self._action_id}")
        self._attr_has_entity_name = False
        self._attr_name = label
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_action_{self._action_id}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name=coordinator.config_entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )

    async def async_press(self) -> None:
        """Execute the action."""
        try:
            await self.coordinator.async_exec_action(self._action_id)
        except BislyApiClientError as exc:
            LOGGER.exception("Failed to execute action %s", self._action_id)
            raise HomeAssistantError(
                translation_domain="hacs_bisly",
                translation_key="action_execute_failed",
            ) from exc


def build_scenario_buttons(coordinator: BislyDataUpdateCoordinator) -> list[BislyScenarioButton]:
    """Build scenario button entities for the given coordinator."""
    data = coordinator.data or {}
    entities: list[BislyScenarioButton] = []
    for action_data in data.get("actions", []):
        action_id = action_data.get("id")
        if action_id is None:
            continue
        desc = ButtonEntityDescription(
            key=f"action_{action_id}",
            translation_key="bisly_action",
        )
        entities.append(BislyScenarioButton(coordinator, desc, action_data))
    return entities
