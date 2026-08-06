"""
Credential validators.

Validation functions for user credentials and authentication.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from custom_components.hacs_bisly.api import BislyApiClient
from homeassistant.helpers.aiohttp_client import async_create_clientsession

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


async def validate_credentials(hass: HomeAssistant, username: str, password: str) -> dict[str]:
    """
    Validate user credentials by performing a Bisly handshake.

    Args:
        hass: Home Assistant instance.
        username: The Bisly username to validate.
        password: The Bisly password to validate.

    Returns:
        The authentication response dict with user_id and server_id.

    Raises:
        BislyApiClientAuthenticationError: If credentials are invalid.
        BislyApiClientCommunicationError: If communication fails.
        BislyApiClientError: For other API errors.
    """
    client = BislyApiClient(
        username=username,
        password=password,
        session=async_create_clientsession(hass),
    )
    try:
        return await client.authenticate()
    finally:
        await client.disconnect()


__all__ = [
    "validate_credentials",
]
