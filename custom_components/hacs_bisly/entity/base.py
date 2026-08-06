"""
Base entity class for hacs_bisly.

All integration entities inherit from this class, which provides
coordinator integration, unique ID generation, and device info.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.hacs_bisly.const import ATTRIBUTION, DOMAIN, MANUFACTURER, MODEL
from custom_components.hacs_bisly.coordinator import BislyDataUpdateCoordinator
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

if TYPE_CHECKING:
    from homeassistant.helpers.entity import EntityDescription


class BislyEntity(CoordinatorEntity[BislyDataUpdateCoordinator]):
    """
    Base entity class for hacs_bisly.

    Provides:
    - Automatic coordinator updates
    - Device info management
    - Unique ID generation
    - Attribution and naming conventions
    """

    _attr_attribution = ATTRIBUTION
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: BislyDataUpdateCoordinator,
        entity_description: EntityDescription,
    ) -> None:
        """
        Initialize the base entity.

        Args:
            coordinator: The data update coordinator.
            entity_description: The entity description.
        """
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._attr_unique_id = f"{coordinator.config_entry.entry_id}_{entity_description.key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, coordinator.config_entry.entry_id)},
            name=coordinator.config_entry.title,
            manufacturer=MANUFACTURER,
            model=MODEL,
        )
