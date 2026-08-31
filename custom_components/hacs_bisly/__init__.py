"""
Custom integration to integrate hacs_bisly with Home Assistant.

Bisly Smart Home Websockets Integration — connects to the Bisly cloud
via NATS WebSocket for real-time monitoring and control of:
- Lighting (on/off, dimming, RGB)
- Climate (heating, cooling, floor heating)
- Ventilation
- Curtains / blinds
- Doors / access control
- Security areas
- Energy counters
- Saunas
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

from webrtc_models import RTCIceServer

from homeassistant.components.web_rtc import async_register_ice_servers
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME, Platform
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv
from homeassistant.loader import async_get_loaded_integration

from .api import BislyApiClient
from .const import (
    DEFAULT_UPDATE_INTERVAL_SECONDS,
    DOMAIN,
    LOGGER,
    MIN_UPDATE_INTERVAL_SECONDS,
    WEBRTC_TURN_CREDENTIAL,
    WEBRTC_TURN_SERVERS,
    WEBRTC_TURN_USERNAME,
)
from .coordinator import BislyDataUpdateCoordinator
from .data import BislyData
from .intercom import BislyIntercomManager

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import BislyConfigEntry

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.CLIMATE,
    Platform.LIGHT,
    Platform.LOCK,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


def _get_webrtc_ice_servers() -> list[RTCIceServer]:
    """Return the Bisly TURN servers as ICE servers for the browser's RTCPeerConnection."""
    return [
        RTCIceServer(urls=[url], username=WEBRTC_TURN_USERNAME, credential=WEBRTC_TURN_CREDENTIAL)
        for url in WEBRTC_TURN_SERVERS
    ]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up the integration — register services and WebRTC ICE servers."""
    # Direct host candidates often fail (browsers mDNS-obfuscate them and HA
    # may run where mDNS doesn't resolve, e.g. containers) — give the browser
    # a TURN relay path on the same infrastructure the integration uses.
    async_register_ice_servers(hass, _get_webrtc_ice_servers)
    return True


async def async_setup_entry(
    hass: HomeAssistant,
    entry: BislyConfigEntry,
) -> bool:
    """
    Set up the Bisly integration from a config entry.

    1. Creates the API client with stored credentials
    2. Initializes the push-based DataUpdateCoordinator
    3. Performs first refresh (authenticate, connect, fetch initial state)
    4. Sets up platforms
    5. Registers the shutdown listener

    Args:
        hass: The Home Assistant instance.
        entry: The config entry being set up.

    Returns:
        True if setup was successful.
    """
    # Initialize API client (username/password from config flow)
    client = BislyApiClient(
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        session=async_get_clientsession(hass),
    )

    # Push-based coordinator — polls controller_list to sync state changes
    # from other clients (Bisly app). Broadcasts are session-scoped on this
    # server and don't arrive for app changes.
    update_interval_hours = entry.options.get("update_interval_hours")
    if update_interval_hours is not None:
        update_seconds = max(update_interval_hours * 3600, MIN_UPDATE_INTERVAL_SECONDS)
    else:
        update_seconds = DEFAULT_UPDATE_INTERVAL_SECONDS

    coordinator = BislyDataUpdateCoordinator(
        hass=hass,
        logger=LOGGER,
        name=DOMAIN,
        config_entry=entry,
        update_interval=timedelta(seconds=update_seconds),
        always_update=False,
    )

    intercom = BislyIntercomManager(hass, entry)

    # Store runtime data
    entry.runtime_data = BislyData(
        client=client,
        integration=async_get_loaded_integration(hass, entry.domain),
        coordinator=coordinator,
        intercom=intercom,
    )

    # First refresh: authenticate, connect NATS, fetch initial state
    await coordinator.async_config_entry_first_refresh()

    # Forward setup to platforms
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register shutdown and reload listeners
    entry.async_on_unload(entry.add_update_listener(async_reload_entry))
    entry.async_on_unload(coordinator.async_shutdown)
    entry.async_on_unload(intercom.teardown)

    return True


async def async_unload_entry(
    hass: HomeAssistant,
    entry: BislyConfigEntry,
) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.client.disconnect()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_reload_entry(
    hass: HomeAssistant,
    entry: BislyConfigEntry,
) -> None:
    """Reload a config entry."""
    await hass.config_entries.async_reload(entry.entry_id)
