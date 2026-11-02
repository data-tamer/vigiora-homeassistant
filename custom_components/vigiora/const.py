"""Constants for the Vigiora integration."""

from __future__ import annotations

from datetime import timedelta
from typing import Final
from urllib.parse import urlsplit

DOMAIN: Final = "vigiora"
MANUFACTURER: Final = "Data Tamer"

CONF_API_KEY: Final = "api_key"
CONF_URL: Final = "url"
CONF_VERIFY_SSL: Final = "verify_ssl"

# Options
CONF_CAMERAS: Final = "cameras"
CONF_EVENT_TYPES: Final = "event_types"
CONF_ALERTS_ONLY: Final = "alerts_only"

EVENT_TYPE_ALERT: Final = "alert"
EVENT_TYPE_SCENE: Final = "scene_description"
EVENT_TYPES: Final = [EVENT_TYPE_ALERT, EVENT_TYPE_SCENE]
DEFAULT_EVENT_TYPES: Final = [EVENT_TYPE_ALERT, EVENT_TYPE_SCENE]

# Polling: the whole state every 60 s (and the event fallback when the SSE
# stream is down); motion levels have no event, so they are polled faster.
SCAN_INTERVAL: Final = timedelta(seconds=60)
MOTION_SCAN_INTERVAL: Final = timedelta(seconds=10)
# A motion sample older than this is treated as "no motion".
MOTION_MAX_AGE: Final = timedelta(seconds=90)
# How long "person in view" stays on after the last evidence of a person.
PERSON_HOLD: Final = timedelta(seconds=120)
# The SSE stream counts as healthy while a frame (heartbeats every 15 s)
# arrived within this window.
STREAM_HEALTHY_WINDOW: Final = timedelta(seconds=60)

SSE_BACKOFF_INITIAL: Final = 1.0
SSE_BACKOFF_MAX: Final = 300.0

ROOM_AI_MODES: Final = ["live", "guardian", "both"]

# Event types consumed from /api/v1/events/stream and the scope each needs
# (besides events:read).
STREAM_EVENT_SCOPES: Final = {
    "alert.created": "alerts:read",
    "alert.acknowledged": "alerts:read",
    "alert.unacknowledged": "alerts:read",
    "guardian.event": "guardian:read",
    "camera.online": "cameras:read",
    "camera.offline": "cameras:read",
    "settings.changed": "settings:read",
}

# Added with the Things model (API 1.17.0+): pushed motion and presence.
THINGS_EVENT_SCOPES: Final = {
    "motion.started": "cameras:read",
    "motion.ended": "cameras:read",
    "presence.changed": "cameras:read",
    "recording.started": "recordings:read",
    "recording.stopped": "recordings:read",
}

# Live video links are refreshed this long before they expire.
STREAM_URL_TTL: Final = 600
STREAM_URL_REFRESH_MARGIN: Final = timedelta(seconds=90)

SIGNAL_EVENT: Final = "vigiora_event_{}"

SERVICE_SPEAK: Final = "speak"
SERVICE_PTZ_GOTO_PRESET: Final = "ptz_goto_preset"
SERVICE_PTZ_MOVE: Final = "ptz_move"
SERVICE_DESCRIBE_CAMERA: Final = "describe_camera"
SERVICE_DETECT_OBJECTS: Final = "detect_objects"
SERVICE_ACKNOWLEDGE_ALERT: Final = "acknowledge_alert"

ATTR_CONFIG_ENTRY_ID: Final = "config_entry_id"
ATTR_CAMERA: Final = "camera"
ATTR_CAMERA_ID: Final = "camera_id"
ATTR_ROOM_ID: Final = "room_id"
ATTR_OUTPUT_ID: Final = "output_id"
ATTR_TEXT: Final = "text"
ATTR_VOLUME: Final = "volume"
ATTR_PRESET: Final = "preset"
ATTR_PAN: Final = "pan"
ATTR_TILT: Final = "tilt"
ATTR_ZOOM: Final = "zoom"
ATTR_DURATION_MS: Final = "duration_ms"
ATTR_PROMPTS: Final = "prompts"
ATTR_INCLUDE_OBJECTS: Final = "include_objects"
ATTR_MIN_CONFIDENCE: Final = "min_confidence"
ATTR_ALERT_ID: Final = "alert_id"


def server_name(url: str) -> str:
    """A display name from the URL: <site> of <site>.vigiora.com, else the host.

    The fallback when the server's own name (GET /api/v1/server) is not readable.
    """
    host = urlsplit(url).hostname or url
    if host.endswith(".vigiora.com"):
        labels = host.removesuffix(".vigiora.com").split(".")
        # lan.<site>.vigiora.com is the site's LAN address.
        return labels[-1]
    return host


def default_title(name: str) -> str:
    """The config entry's title for a server name."""
    return f"Vigiora {name}"
