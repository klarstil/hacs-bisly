"""Diagnostics support for hacs_bisly."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.redact import async_redact_data

from .const import CONF_AUTH_HASH

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

    from .data import BislyConfigEntry

TO_REDACT = {
    CONF_PASSWORD,
    CONF_USERNAME,
    CONF_AUTH_HASH,
    "username",
    "password",
    "auth_hash",
    "token",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: BislyConfigEntry,
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data.coordinator
    client = entry.runtime_data.client
    integration = entry.runtime_data.integration

    # Device/entity registry
    device_reg = dr.async_get(hass)
    entity_reg = er.async_get(hass)
    devices = dr.async_entries_for_config_entry(device_reg, entry.entry_id)

    device_info = []
    for device in devices:
        entities = er.async_entries_for_device(entity_reg, device.id)
        device_info.append(
            {
                "id": device.id,
                "name": device.name,
                "manufacturer": device.manufacturer,
                "model": device.model,
                "sw_version": device.sw_version,
                "entity_count": len(entities),
                "entities": [
                    {
                        "entity_id": entity.entity_id,
                        "platform": entity.platform,
                        "original_name": entity.original_name,
                        "disabled": entity.disabled,
                        "disabled_by": entity.disabled_by.value if entity.disabled_by else None,
                    }
                    for entity in entities
                ],
            }
        )

    coordinator_info = {
        "last_update_success": coordinator.last_update_success,
        "data_keys": list(coordinator.data.keys()) if isinstance(coordinator.data, dict) else None,
    }

    api_info = {
        "has_credentials": bool(client._username),  # noqa: SLF001
        "connected": client._transport.connected if hasattr(client, "_transport") else False,  # noqa: SLF001
        "server_id": client.server_id,
        "user_id": client.user_id,
    }

    integration_info = {
        "name": integration.name,
        "version": integration.version,
        "domain": integration.domain,
        "documentation": integration.documentation,
        "issue_tracker": integration.issue_tracker,
    }

    entry_info = {
        "entry_id": entry.entry_id,
        "version": entry.version,
        "minor_version": entry.minor_version,
        "domain": entry.domain,
        "title": entry.title,
        "state": str(entry.state),
        "unique_id": entry.unique_id,
        "disabled_by": entry.disabled_by.value if entry.disabled_by else None,
        "data": async_redact_data(entry.data, TO_REDACT),
        "options": async_redact_data(entry.options, TO_REDACT),
    }

    error_info = {
        "last_exception": str(coordinator.last_exception) if coordinator.last_exception else None,
        "last_exception_type": type(coordinator.last_exception).__name__ if coordinator.last_exception else None,
    }

    return {
        "entry": entry_info,
        "integration": integration_info,
        "coordinator": coordinator_info,
        "api": api_info,
        "devices": device_info,
        "error": error_info,
    }
