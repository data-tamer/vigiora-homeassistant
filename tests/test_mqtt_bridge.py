"""The MQTT discovery adapter (contrib/mqtt_bridge): a second table over vigiora_things."""

from __future__ import annotations

import importlib.util
import json
import pathlib
import sys
from typing import Any

import pytest

from .conftest import THINGS

_PATH = pathlib.Path(__file__).parent.parent / "contrib" / "mqtt_bridge" / "vigiora_mqtt.py"


@pytest.fixture(scope="module")
def mod() -> Any:
    spec = importlib.util.spec_from_file_location("vigiora_mqtt", _PATH)
    m = importlib.util.module_from_spec(spec)
    sys.modules["vigiora_mqtt"] = m
    spec.loader.exec_module(m)
    return m


class FakeBroker:
    """Records what the bridge publishes and subscribes to."""

    def __init__(self) -> None:
        self.published: list[tuple[str, str, bool]] = []
        self.subscriptions: list[str] = []

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        self.published.append((topic, payload, retain))

    async def subscribe(self, topic: str) -> None:
        self.subscriptions.append(topic)

    def last(self, topic: str) -> str | None:
        for t, p, _ in reversed(self.published):
            if t == topic:
                return p
        return None


class FakeThings:
    """The Things API the bridge reads and commands."""

    def __init__(self, mod: Any) -> None:
        self.mod = mod
        self.data = json.loads(json.dumps(THINGS))
        self.commands: list[tuple[str, str, dict]] = []

    async def things(self) -> list[Any]:
        return [self.mod.vt.Thing.from_dict(t) for t in self.data]

    async def thing(self, thing_id: str) -> Any:
        return next(self.mod.vt.Thing.from_dict(t) for t in self.data if t["id"] == thing_id)

    async def command(self, thing_id: str, command: str, params: dict) -> Any:
        self.commands.append((thing_id, command, params))
        if command == "recording.turn_off":
            cam = next(t for t in self.data if t["id"] == thing_id)
            for c in cam["capabilities"]:
                if c["name"] == "recording":
                    c["state"]["on"] = False
        return {"ok": True}


async def _bridge(mod: Any) -> tuple[Any, FakeBroker, FakeThings]:
    broker, api = FakeBroker(), FakeThings(mod)
    bridge = mod.Bridge(api, broker, node_id="srv1")
    await bridge.start()
    return bridge, broker, api


async def test_discovery_and_state(mod: Any) -> None:
    bridge, broker, _ = await _bridge(mod)
    configs = {t: json.loads(p) for t, p, r in broker.published if t.endswith("/config") and r}
    motion = configs["homeassistant/binary_sensor/srv1/camera_1_motion/config"]
    assert motion["device_class"] == "motion"
    assert motion["state_topic"] == "vigiora/srv1/camera:1/motion/active"
    assert motion["unique_id"] == "vigiora_srv1_camera_1_motion"
    assert motion["device"]["identifiers"] == ["vigiora_srv1_camera_1"]
    assert motion["device"]["via_device"] == "vigiora_srv1_server"
    rec = configs["homeassistant/switch/srv1/camera_1_recording/config"]
    assert rec["command_topic"] == "vigiora/srv1/camera:1/recording/set"
    sel = configs["homeassistant/select/srv1/room_7_ai_mode/config"]
    assert sel["options"] == ["live", "guardian", "both"]
    assert "homeassistant/sensor/srv1/area_1_occupancy/config" in configs
    assert "homeassistant/sensor/srv1/server_unacknowledged_alerts/config" in configs
    # The HA-only capabilities (camera, events) are not in this table.
    assert not any("/camera/" in t for t in configs)
    # Retained state and availability.
    assert broker.last("vigiora/srv1/camera:1/motion/active") == "OFF"
    assert broker.last("vigiora/srv1/camera:2/device/online") == "OFF"
    assert broker.last("vigiora/srv1/area:1/occupancy/count") == "3"
    assert broker.last("vigiora/srv1/server/alert/unacknowledged") == "746"
    assert broker.last("vigiora/srv1/status") == "online"
    assert broker.subscriptions == ["vigiora/srv1/+/+/set"]
    await bridge.stop()
    assert broker.last("vigiora/srv1/status") == "offline"


async def test_events_update_state_and_forward(mod: Any) -> None:
    bridge, broker, _ = await _bridge(mod)
    ev = mod.vt.ThingEvent(
        id="01A",
        type="vigiora.motion.started",
        subject="camera:1",
        time="2099-01-01T00:00:00Z",
        data={"started_at": "x"},
        capability="motion",
    )
    await bridge.on_event(ev)
    assert broker.last("vigiora/srv1/camera:1/motion/active") == "ON"
    alert = mod.vt.ThingEvent(
        id="01B",
        type="vigiora.alert.created",
        subject="camera:1",
        time=None,
        data={"alert_id": 9, "label": "person"},
        capability="alert",
    )
    await bridge.on_event(alert)
    fwd = json.loads(broker.last("vigiora/srv1/camera:1/events"))
    assert fwd["type"] == "vigiora.alert.created" and fwd["data"]["alert_id"] == 9
    assert broker.published[-1][2] is False  # events are not retained


async def test_commands_from_mqtt(mod: Any) -> None:
    bridge, broker, api = await _bridge(mod)
    await bridge.handle_message("vigiora/srv1/camera:1/recording/set", "OFF")
    assert api.commands == [("camera:1", "recording.turn_off", {})]
    assert broker.last("vigiora/srv1/camera:1/recording/on") == "OFF"
    await bridge.handle_message("vigiora/srv1/room:7/ai_mode/set", "guardian")
    assert api.commands[-1] == ("room:7", "ai_mode.set", {"mode": "guardian"})
    # Invalid values and unknown topics do nothing.
    await bridge.handle_message("vigiora/srv1/room:7/ai_mode/set", "disco")
    await bridge.handle_message("vigiora/srv1/camera:9/recording/set", "ON")
    await bridge.handle_message("other/topic/set", "ON")
    assert len(api.commands) == 2


def test_same_library_two_tables(mod: Any) -> None:
    """The MQTT table and the HA table are different rows over the same Rule type and planner."""
    from custom_components.vigiora.things_map import HA_TABLE

    things = [mod.vt.Thing.from_dict(t) for t in THINGS]
    mqtt = {(p.thing_id, p.rule.platform) for p in mod.vt.plan(things, mod.MQTT_TABLE)}
    ha = {(p.thing_id, p.rule.platform) for p in mod.vt.plan(things, HA_TABLE)}
    assert ("camera:1", "camera") in ha and ("camera:1", "camera") not in mqtt
    assert ("camera:1", "binary_sensor") in ha & mqtt
