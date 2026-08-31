"""Intercom binary sensors for hacs_bisly.

Provides binary sensors for the Bisly intercom:
- Intercom ringing (incoming call active/ringing)
- Doorbell pressed (momentary, stays on for a few seconds)
"""

from __future__ import annotations

import time

from custom_components.hacs_bisly.entity.base import BislyEntity
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

PARALLEL_UPDATES = 0

DOORBELL_ON_SECONDS = 5.0


class BislyIntercomRingingSensor(BislyEntity, BinarySensorEntity):
    """Binary sensor that reports an incoming intercom call."""

    _attr_device_class = BinarySensorDeviceClass.SOUND
    _attr_icon = "mdi:phone-ring"

    @property
    def is_on(self) -> bool:
        """Return True if the intercom is ringing or in an active call."""
        intercom = (self.coordinator.data or {}).get("intercom", {})
        return intercom.get("state") in ("ringing", "active")


class BislyDoorbellSensor(BislyEntity, BinarySensorEntity):
    """Binary sensor that reports a doorbell press (momentary)."""

    _attr_device_class = BinarySensorDeviceClass.SOUND
    _attr_icon = "mdi:bell-ring"

    @property
    def is_on(self) -> bool:
        """Return True if the doorbell was pressed within the last seconds."""
        intercom = (self.coordinator.data or {}).get("intercom", {})
        pressed_at = intercom.get("doorbell_at") or 0.0
        return bool(pressed_at) and (time.monotonic() - pressed_at) < DOORBELL_ON_SECONDS
