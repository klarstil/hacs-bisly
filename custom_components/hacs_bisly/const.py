"""Constants for hacs_bisly."""

from logging import Logger, getLogger

LOGGER: Logger = getLogger(__package__)

# Integration metadata
DOMAIN = "hacs_bisly"
ATTRIBUTION = "Data provided by Bisly"

# Platform parallel updates - 0 for push-based (cloud_push) architectures
PARALLEL_UPDATES = 0

# Bisly NATS WebSocket configuration
BISLY_WS_URL = "wss://cloud.bisly.ee:8223"
BISLY_NATS_USER = "mobile"
BISLY_NATS_PASS = "peeterpaan"

# NATS protocol constants
NATS_CONNECT_TEMPLATE = '{{"verbose":false,"pedantic":false,"user":"{user}","pass":"{password}","echo":false}}'
NATS_PING_INTERVAL = 30  # seconds between NATS pings

# Command types (matching Bisly app CommandType enum)
CMD_CONTROLLER_LIST = "controller_list"
CMD_LIGHT = "light"
CMD_CLIMATE = "climate"
CMD_VENTILATION = "centralfan"
CMD_CURTAINS = "curtain"
CMD_SAUNA = "sauna"
CMD_RGB = "rgb"

COMMAND_LIGHT = "light"
COMMAND_PIR = "pir"
COMMAND_DEVICE = "device"
COMMAND_RGB = "rgb"
COMMAND_CLIMATE = "climate"
COMMAND_CURTAIN = "curtain"
COMMAND_SAUNA = "sauna"
COMMAND_VENTILATION = "centralfan"
COMMAND_KEYCARDS = "keycards"
COMMAND_SECURITY = "security"
COMMAND_USER = "user"
COMMAND_USER_EMAIL = "user_email"
COMMAND_ACTIONS = "actions"
COMMAND_HISTORY = "history"
COMMAND_DOORS = "doors"
COMMAND_ROOMS = "rooms"
COMMAND_SERVERS = "servers"
COMMAND_ACCOUNT = "account"
COMMAND_LOG = "log"
COMMAND_SCENARIO = "scenario"
COMMAND_AWAY = "away"
COMMAND_STATE = "state"
COMMAND_VIDEOSERVER = "videoserver"

# Camera controller type (for controller_list type="14")
CTRL_TYPE_CAMERA = "14"

# Camera image CDN URL template (Bisly app pattern)
CAMERA_IMAGE_URL = "https://cloud.bisly.ee/image/{server_id}/{camera_id}"
CAMERA_IMAGE_CACHE_WINDOW = 30  # seconds, cache-buster rounding window
ACTION_GET = "get"
ACTION_SET = "set"
ACTION_EXEC = "exec"
ACTION_LIST = "list"
ACTION_ADD = "add"
ACTION_DELETE = "delete"
ACTION_UPDATE = "update"
ACTION_MERGE = "merge"

# Lighting device types
LIGHTING_REGULAR_TYPES = {"0", "2", "3", "9"}
LIGHTING_PIR_TYPES = {"5", "7"}
LIGHTING_RGB_TYPES = {"8", "6"}
LIGHTING_SLIDER_TYPES = {"1", "4"}

# Doors sub-command types
DOORS_GET_AREAS = "10"
DOORS_GET_AREAS_WITH_PIN = "11"
DOORS_ARM_AREA = "12"
DOORS_DISARM_AREA = "13"
DOORS_MANAGE_PIN = "14"
DOORS_GET_PIN_STATUS = "15"

# Device kind taxonomy
KIND_LIGHTING_DEVICE = "lighting.device"
KIND_LIGHTING_SCENE = "lighting.scene"
KIND_CLIMATE_ROOM = "climate.room"
KIND_CURTAIN_DEVICE = "curtains.device"
KIND_VENTILATION_DEVICE = "ventilation.device"
KIND_SAUNA_DEVICE = "sauna.device"
KIND_ACCESS_DOOR = "access.door"
KIND_ACCESS_CAMERA = "access.camera"

# NATS subjects
SUBJECT_CLOUD_AUTH = "cloud.auth"
SUBJECT_CLOUD_SERVERS = "cloud.servers"
SUBJECT_CLOUD_EMAIL = "cloud.email"
SUBJECT_COMMANDS_CLOUD = "commands.cloud"
SUBJECT_COMMANDS = "commands"
SUBJECT_BROADCAST = "broadcast"
SUBJECT_ACCOUNT = "account"
SUBJECT_LOG = "log"

# Subject routing: maps command name → NATS subject template
# "{serverID}" is replaced with the actual server ID
SUBJECT_ROUTING: dict[str, str] = {
    "handshake": "cloud.auth",
    "servers": "cloud.servers",
    "user_settings": "commands.cloud",
    "user_email": "cloud.email",
}

# WebRTC TURN servers (matches Bisly app configuration)
WEBRTC_TURN_SERVERS: list[str] = [
    "turn:46.22.210.59:19302",
    "turn:51.120.68.174:19302",
]
WEBRTC_TURN_USERNAME = "test"
WEBRTC_TURN_CREDENTIAL = "test"

# Config entry data keys (username/password from homeassistant.const.CONF_USERNAME/CONF_PASSWORD)
CONF_SERVER_ID = "server_id"
CONF_USER_ID = "user_id"
CONF_AUTH_HASH = "auth_hash"

# Options defaults
DEFAULT_UPDATE_INTERVAL_HOURS = 6  # Legacy — replaced by DEFAULT_UPDATE_INTERVAL_SECONDS
DEFAULT_UPDATE_INTERVAL_SECONDS = 60
MIN_UPDATE_INTERVAL_SECONDS = 5
DEFAULT_ENABLE_DEBUGGING = False
CAMERA_REFRESH_INTERVAL_SECONDS = 3600  # 1 hour — cameras rarely change

# Bisly device info
MANUFACTURER = "Bisly"
MODEL = "Bisly Smart Home"
