"""The Home Assistant mapping of Vigiora things: one table.

Every capability that becomes an entity is one row: the HA platform, the
entity key (part of the unique id, the same keys as 0.1.0 so entities keep
their ids), the state field, the device class and the commands. Platforms
build their entities from this table when the server has the Things model
(API 1.18.0); a second adapter (MQTT discovery) only needs its own table
(contrib/mqtt_bridge/vigiora_mqtt.py).

Capabilities that are actions rather than entities map to services:
speak → vigiora.speak, ptz → vigiora.ptz_move / ptz_goto_preset,
describe → vigiora.describe_camera, detect_objects → vigiora.detect_objects,
alert.acknowledge → vigiora.acknowledge_alert. video_stream and snapshot map
to the camera entity (camera.py).
"""

from __future__ import annotations

from .vigiora_things import Rule
from .vigiora_things.models import (
    KIND_AREA,
    KIND_CAMERA,
    KIND_MICROPHONE,
    KIND_ROOM,
    KIND_SERVER,
)

HA_TABLE: tuple[Rule, ...] = (
    # binary_sensor
    Rule(
        "motion",
        "binary_sensor",
        "motion",
        (KIND_CAMERA,),
        field="active",
        device_class="motion",
        translation_key="motion",
        events=("vigiora.motion.started", "vigiora.motion.ended"),
    ),
    Rule(
        "presence",
        "binary_sensor",
        "person",
        (KIND_CAMERA,),
        field="person",
        device_class="occupancy",
        translation_key="person",
        events=("vigiora.presence.changed",),
    ),
    Rule(
        "device",
        "binary_sensor",
        "online",
        (KIND_CAMERA, KIND_MICROPHONE),
        field="online",
        device_class="connectivity",
        entity_category="diagnostic",
        translation_key="online",
        events=("vigiora.device.online", "vigiora.device.offline"),
    ),
    # sensor
    Rule(
        "alert",
        "sensor",
        "unacknowledged_alerts",
        (KIND_SERVER,),
        field="unacknowledged",
        state_class="measurement",
        translation_key="unacknowledged_alerts",
    ),
    Rule(
        "occupancy",
        "sensor",
        "occupancy",
        (KIND_AREA,),
        field="count",
        state_class="measurement",
        translation_key="area_occupancy",
    ),
    Rule(
        "counter",
        "sensor",
        "visitors_today",
        (KIND_AREA,),
        field="in_today",
        state_class="total",
        translation_key="area_visitors_today",
    ),
    # switch
    Rule(
        "recording",
        "switch",
        "recording",
        (KIND_CAMERA, KIND_MICROPHONE),
        field="on",
        entity_category="config",
        translation_key="recording",
        on_command="recording.turn_on",
        off_command="recording.turn_off",
        requires_scope="cameras:write",
        events=("vigiora.recording.started", "vigiora.recording.stopped"),
    ),
    Rule(
        "ai_guardian",
        "switch",
        "ai_guardian",
        (KIND_CAMERA,),
        field="on",
        entity_category="config",
        translation_key="ai_guardian",
        on_command="ai_guardian.turn_on",
        off_command="ai_guardian.turn_off",
        requires_scope="cameras:write",
    ),
    # select
    Rule(
        "ai_mode",
        "select",
        "ai_mode",
        (KIND_ROOM,),
        field="mode",
        entity_category="config",
        translation_key="ai_mode",
        set_command="ai_mode.set",
        set_param="mode",
        options=("live", "guardian", "both"),
        requires_scope="cameras:write",
    ),
    # event (alerts, detections, Guardian descriptions)
    Rule(
        "alert",
        "event",
        "events",
        (KIND_CAMERA, KIND_MICROPHONE),
        translation_key="guardian",
        events=(
            "vigiora.alert.created",
            "vigiora.object_detection.detected",
            "vigiora.ai_guardian.observed",
        ),
    ),
    # camera (live video and snapshots)
    Rule("snapshot", "camera", "camera", (KIND_CAMERA,), translation_key="camera"),
)

# The unique id prefix of each thing kind (0.1.0 ids: <entry>_camera_<id>_<key>,
# <entry>_room_<id>_<key>, <entry>_area_<id>_<key>, <entry>_<key> for the server).
_UNIQUE_KIND = {
    KIND_CAMERA: "camera",
    KIND_MICROPHONE: "camera",
    KIND_ROOM: "room",
    KIND_AREA: "area",
}


def unique_id(entry_id: str, thing_id: str, key: str) -> str:
    """The entity's unique id (compatible with 0.1.0)."""
    kind, _, raw = thing_id.partition(":")
    if kind == KIND_SERVER or not raw:
        return f"{entry_id}_{key}"
    return f"{entry_id}_{_UNIQUE_KIND.get(kind, kind)}_{raw}_{key}"


def rules(platform: str) -> tuple[Rule, ...]:
    """The table's rows for one platform."""
    return tuple(r for r in HA_TABLE if r.platform == platform)
