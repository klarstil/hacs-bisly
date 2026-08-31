"""Shared constants for the Bisly stream bridge.

Values are ported from custom_components/hacs_bisly/const.py and kept
standalone so the add-on has no Home Assistant dependency.
"""

from __future__ import annotations

import logging

LOGGER = logging.getLogger("bisly_bridge")

# Bisly NATS WebSocket configuration
BISLY_WS_URL = "wss://cloud.bisly.ee:8223"
BISLY_NATS_USER = "mobile"
BISLY_NATS_PASS = "peeterpaan"

# NATS protocol constants
NATS_CONNECT_TEMPLATE = '{{"verbose":false,"pedantic":false,"user":"{user}","pass":"{password}","echo":false}}'

# NATS subjects
SUBJECT_BROADCAST = "broadcast"
SUBJECT_ROUTING: dict[str, str] = {
    "handshake": "cloud.auth",
}

# Camera controller type for controller_list (14 = VIDEOPHONE)
CTRL_TYPE_CAMERA = "14"

# WebRTC TURN servers (matches Bisly app configuration)
WEBRTC_TURN_SERVERS: list[str] = [
    "turn:46.22.210.59:19302",
    "turn:51.120.68.174:19302",
]
WEBRTC_TURN_USERNAME = "test"
WEBRTC_TURN_CREDENTIAL = "test"
