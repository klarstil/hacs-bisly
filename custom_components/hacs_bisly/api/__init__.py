"""
API package for hacs_bisly.

Architecture:
    Three-layer data flow: Entities → Coordinator → API Client.
    Only the coordinator should call the API client. Entities must never
    import or call the API client directly.

Exception hierarchy:
    BislyApiClientError (base)
    ├── BislyApiClientCommunicationError (network/timeout)
    └── BislyApiClientAuthenticationError (auth failures)

Coordinator exception mapping:
    ApiClientAuthenticationError → ConfigEntryAuthFailed (triggers reauth)
    ApiClientCommunicationError → UpdateFailed (auto-retry)
    ApiClientError             → UpdateFailed (auto-retry)
"""

from .client import (
    BislyApiClient,
    BislyApiClientAuthenticationError,
    BislyApiClientCommunicationError,
    BislyApiClientError,
)
from .nats_transport import BislyNATSConnectionError, BislyNATSTransport, BislyNATSTransportError

__all__ = [
    "BislyApiClient",
    "BislyApiClientAuthenticationError",
    "BislyApiClientCommunicationError",
    "BislyApiClientError",
    "BislyNATSConnectionError",
    "BislyNATSTransport",
    "BislyNATSTransportError",
]
