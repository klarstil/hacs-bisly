"""Binary sensor platform for hacs_bisly."""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.hacs_bisly.const import PARALLEL_UPDATES as PARALLEL_UPDATES
from homeassistant.components.binary_sensor import BinarySensorEntityDescription

from .connectivity import ENTITY_DESCRIPTIONS as CONNECTIVITY_DESCRIPTIONS, BislyConnectivitySensor
from .intercom import BislyDoorbellSensor, BislyIntercomRingingSensor

if TYPE_CHECKING:
    from custom_components.hacs_bisly.data import BislyConfigEntry
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddEntitiesCallback

# Combine all entity descriptions from different modules
ENTITY_DESCRIPTIONS: tuple[BinarySensorEntityDescription, ...] = CONNECTIVITY_DESCRIPTIONS


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BislyConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the binary_sensor platform."""
    coordinator = entry.runtime_data.coordinator

    connectivity_entities = [
        BislyConnectivitySensor(
            coordinator=coordinator,
            entity_description=entity_description,
        )
        for entity_description in CONNECTIVITY_DESCRIPTIONS
    ]

    intercom_entities = [
        BislyIntercomRingingSensor(
            coordinator=coordinator,
            entity_description=BinarySensorEntityDescription(
                key="intercom_ringing",
                translation_key="intercom_ringing",
            ),
        ),
        BislyDoorbellSensor(
            coordinator=coordinator,
            entity_description=BinarySensorEntityDescription(
                key="intercom_doorbell",
                translation_key="intercom_doorbell",
            ),
        ),
    ]

    async_add_entities(connectivity_entities + intercom_entities)
