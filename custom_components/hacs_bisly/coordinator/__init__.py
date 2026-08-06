"""
Data update coordinator package for hacs_bisly.

Provides the BislyDataUpdateCoordinator that manages the persistent NATS
WebSocket connection, periodic polling, and broadcast-based state updates.

https://developers.home-assistant.io/docs/integration_fetching_data
"""

from __future__ import annotations

from .base import BislyDataUpdateCoordinator

__all__ = ["BislyDataUpdateCoordinator"]
