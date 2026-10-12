"""
Custom types for hacs_bisly.

This module defines the runtime data structure attached to each config entry.
Access pattern: entry.runtime_data.client / entry.runtime_data.coordinator

The BislyConfigEntry type alias is used throughout the integration
for type-safe access to the config entry's runtime data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from homeassistant.util.hass_dict import HassKey

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.loader import Integration

    from .api import BislyApiClient
    from .coordinator import BislyDataUpdateCoordinator
    from .intercom import BislyIntercomManager


type BislyConfigEntry = ConfigEntry[BislyData]


@dataclass
class StreamBridgeInfo:
    """Connection details for the bisly-stream-bridge app.

    Published by the app through Supervisor add-on discovery and stored on
    ``hass.data[DATA_STREAM_BRIDGE]`` while the bridge config entry is loaded.
    """

    host: str
    port: int
    cameras: dict[str, str] = field(default_factory=dict)

    def rtsp_url(self, camera_id: str) -> str | None:
        """Return the RTSP URL for a camera id, or None when the bridge has no mapping."""
        slug = self.cameras.get(camera_id)
        if slug is None:
            return None
        return f"rtsp://{self.host}:{self.port}/{slug}"


DATA_STREAM_BRIDGE: HassKey[StreamBridgeInfo] = HassKey("hacs_bisly_stream_bridge")


@dataclass
class BislyData:
    """Runtime data for hacs_bisly config entries.

    Stored as entry.runtime_data after successful setup.
    Provides typed access to the API client and coordinator instances.
    """

    client: BislyApiClient
    coordinator: BislyDataUpdateCoordinator
    integration: Integration
    intercom: BislyIntercomManager
