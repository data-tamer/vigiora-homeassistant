r"""Vigiora things → MQTT with Home Assistant MQTT discovery (a proof of reuse).

A second platform adapter over the same `vigiora_things` library the Home
Assistant integration uses: only the mapping table (MQTT_TABLE) and the MQTT
plumbing are specific to MQTT. Any MQTT consumer works; Home Assistant's MQTT
integration discovers the entities from the retained config topics.

Topics (node = the server id):
  <discovery>/<component>/<node>/<thing>_<key>/config   retained discovery config
  vigiora/<node>/<thing>/<capability>/<field>           retained state
  vigiora/<node>/<thing>/<key>/set                      commands (switch ON/OFF, select value)
  vigiora/<node>/<thing>/events                         events (alerts, Guardian), not retained
  vigiora/<node>/status                                 online / offline (availability)

Run (a proof, not a product; needs `pip install aiomqtt`):
  VIGIORA_API_KEY=vgk_… python contrib/mqtt_bridge/vigiora_mqtt.py \\
      --url https://site.vigiora.com --broker localhost
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import logging
import os
import pathlib
import sys
from typing import Any, Protocol

_LIB = (
    pathlib.Path(__file__).resolve().parents[2] / "custom_components" / "vigiora" / "vigiora_things"
)


def _load_library() -> Any:
    """Import vigiora_things (installed, or from this repository without Home Assistant)."""
    if "vigiora_things" in sys.modules:
        return sys.modules["vigiora_things"]
    spec = importlib.util.spec_from_file_location(
        "vigiora_things", _LIB / "__init__.py", submodule_search_locations=[str(_LIB)]
    )
    if spec is None or spec.loader is None:
        raise ImportError("vigiora_things not found")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["vigiora_things"] = mod
    spec.loader.exec_module(mod)
    return mod


vt = _load_library()
Rule = vt.Rule

_LOGGER = logging.getLogger("vigiora_mqtt")

# The MQTT mapping table: capability → MQTT discovery component.
MQTT_TABLE: tuple[Any, ...] = (
    Rule("motion", "binary_sensor", "motion", ("camera",), field="active", device_class="motion"),
    Rule(
        "presence", "binary_sensor", "person", ("camera",), field="person", device_class="occupancy"
    ),
    Rule(
        "device",
        "binary_sensor",
        "online",
        ("camera", "microphone"),
        field="online",
        device_class="connectivity",
        entity_category="diagnostic",
    ),
    Rule(
        "recording",
        "switch",
        "recording",
        ("camera", "microphone"),
        field="on",
        entity_category="config",
        on_command="recording.turn_on",
        off_command="recording.turn_off",
    ),
    Rule(
        "ai_guardian",
        "switch",
        "ai_guardian",
        ("camera",),
        field="on",
        entity_category="config",
        on_command="ai_guardian.turn_on",
        off_command="ai_guardian.turn_off",
    ),
    Rule(
        "ai_mode",
        "select",
        "ai_mode",
        ("room",),
        field="mode",
        set_command="ai_mode.set",
        set_param="mode",
        options=("live", "guardian", "both"),
    ),
    Rule(
        "occupancy",
        "sensor",
        "occupancy",
        ("area",),
        field="count",
        state_class="measurement",
        unit="people",
    ),
    Rule(
        "counter",
        "sensor",
        "visitors_today",
        ("area",),
        field="in_today",
        state_class="total",
        unit="people",
    ),
    Rule(
        "alert",
        "sensor",
        "unacknowledged_alerts",
        ("server",),
        field="unacknowledged",
        state_class="measurement",
    ),
)

# Events forwarded to vigiora/<node>/<thing>/events.
FORWARDED_EVENTS = ("alert", "object_detection", "ai_guardian")


class Publisher(Protocol):
    """The MQTT client the bridge needs."""

    async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
        """Publish one message."""

    async def subscribe(self, topic: str) -> None:
        """Subscribe (messages come back through Bridge.handle_message)."""


def _slug(thing_id: str) -> str:
    return thing_id.replace(":", "_")


class Bridge:
    """Things → MQTT discovery, state and commands."""

    def __init__(
        self,
        client: Any,
        publisher: Publisher,
        *,
        node_id: str,
        discovery_prefix: str = "homeassistant",
        base: str = "vigiora",
        table: tuple[Any, ...] = MQTT_TABLE,
    ) -> None:
        """Initialise."""
        self.client = client
        self.pub = publisher
        self.node = node_id
        self.prefix = discovery_prefix
        self.base = f"{base}/{node_id}"
        self.table = table
        self.things: dict[str, Any] = {}
        self.plans: list[Any] = []

    # --- topics ------------------------------------------------------------------

    def state_topic(self, thing_id: str, rule: Any) -> str:
        """Where a rule's state is published."""
        return f"{self.base}/{thing_id}/{rule.capability}/{rule.field}"

    def command_topic(self, thing_id: str, rule: Any) -> str:
        """Where a rule's commands arrive."""
        return f"{self.base}/{thing_id}/{rule.key}/set"

    @property
    def availability_topic(self) -> str:
        """The bridge's online/offline topic."""
        return f"{self.base}/status"

    # --- discovery -----------------------------------------------------------------

    def discovery(self) -> list[tuple[str, dict[str, Any]]]:
        """The retained discovery configs of every planned entity."""
        out = []
        for p in self.plans:
            thing, rule = self.things[p.thing_id], p.rule
            unique = f"vigiora_{self.node}_{_slug(thing.id)}_{rule.key}"
            device_id = f"vigiora_{self.node}_{_slug(thing.id)}"
            cfg: dict[str, Any] = {
                "name": rule.key.replace("_", " ").capitalize(),
                "unique_id": unique,
                "object_id": f"{thing.name}_{rule.key}".lower().replace(" ", "_"),
                "state_topic": self.state_topic(thing.id, rule),
                "availability_topic": self.availability_topic,
                "device": {
                    "identifiers": [device_id],
                    "name": thing.name,
                    "manufacturer": thing.manufacturer or "Vigiora",
                    "model": thing.model or thing.kind.capitalize(),
                },
            }
            if thing.id != "server":
                cfg["device"]["via_device"] = f"vigiora_{self.node}_server"
            for key, value in (
                ("device_class", rule.device_class),
                ("entity_category", rule.entity_category),
                ("state_class", rule.state_class),
                ("unit_of_measurement", rule.unit),
            ):
                if value:
                    cfg[key] = value
            if rule.platform in ("binary_sensor", "switch"):
                cfg["payload_on"], cfg["payload_off"] = "ON", "OFF"
            if rule.platform in ("switch", "select"):
                cfg["command_topic"] = self.command_topic(thing.id, rule)
            if rule.platform == "select":
                cfg["options"] = list(rule.options)
            out.append(
                (
                    f"{self.prefix}/{rule.platform}/{self.node}/{_slug(thing.id)}_{rule.key}/config",
                    cfg,
                )
            )
        return out

    def states(self, thing_id: str | None = None) -> list[tuple[str, str]]:
        """The retained state messages (of one thing, or all)."""
        out = []
        for p in self.plans:
            if thing_id is not None and p.thing_id != thing_id:
                continue
            thing, rule = self.things[p.thing_id], p.rule
            value = rule.value(thing)
            if rule.platform in ("binary_sensor", "switch"):
                payload = "None" if value is None else ("ON" if value else "OFF")
            else:
                payload = "None" if value is None else str(value)
            out.append((self.state_topic(thing.id, rule), payload))
        return out

    # --- lifecycle -----------------------------------------------------------------

    async def refresh(self) -> None:
        """Read every thing and plan its entities."""
        self.things = {t.id: t for t in await self.client.things()}
        self.plans = vt.plan(self.things.values(), self.table)

    async def start(self) -> None:
        """Publish discovery, state and availability; subscribe to commands."""
        await self.refresh()
        for topic, cfg in self.discovery():
            await self.pub.publish(topic, json.dumps(cfg), retain=True)
        for topic, payload in self.states():
            await self.pub.publish(topic, payload, retain=True)
        await self.pub.subscribe(f"{self.base}/+/+/set")
        await self.pub.publish(self.availability_topic, "online", retain=True)

    async def stop(self) -> None:
        """Mark the bridge offline."""
        await self.pub.publish(self.availability_topic, "offline", retain=True)

    # --- events and commands ---------------------------------------------------------

    async def on_event(self, event: Any) -> None:
        """A Things event: update state (retained) and forward alerts."""
        if vt.apply_event(self.things, event) is not None:
            for topic, payload in self.states(event.subject):
                await self.pub.publish(topic, payload, retain=True)
        if event.capability in FORWARDED_EVENTS:
            body = {"id": event.id, "type": event.type, "time": event.time, "data": event.data}
            await self.pub.publish(f"{self.base}/{event.subject}/events", json.dumps(body))

    async def handle_message(self, topic: str, payload: str) -> None:
        """A command from MQTT: run the Things command, then publish the new state."""
        prefix = self.base + "/"
        if not topic.startswith(prefix) or not topic.endswith("/set"):
            return
        thing_id, _, key = topic[len(prefix) : -len("/set")].rpartition("/")
        plan = next((p for p in self.plans if p.thing_id == thing_id and p.rule.key == key), None)
        if plan is None:
            _LOGGER.warning("Unknown command topic %s", topic)
            return
        rule = plan.rule
        if rule.platform == "switch":
            command = rule.on_command if payload.strip().upper() == "ON" else rule.off_command
            params: dict[str, Any] = {}
        elif rule.platform == "select" and payload in rule.options:
            command, params = rule.set_command, {rule.set_param: payload}
        else:
            _LOGGER.warning("Ignoring %r on %s", payload, topic)
            return
        try:
            await self.client.command(thing_id, command, params)
        except vt.ThingsError as err:
            _LOGGER.warning("Command %s on %s failed: %s", command, thing_id, err)
            return
        self.things[thing_id] = await self.client.thing(thing_id)
        for t, p in self.states(thing_id):
            await self.pub.publish(t, p, retain=True)

    async def run(self) -> None:
        """Start, then follow the event stream forever."""
        await self.start()
        sub = vt.EventSubscription(self.client, self.on_event, things_format=True)
        try:
            await sub.run()
        finally:
            await self.stop()


async def _main() -> None:  # pragma: no cover - needs a broker
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--url", required=True)
    parser.add_argument("--broker", default="localhost")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--discovery-prefix", default="homeassistant")
    args = parser.parse_args()
    key = os.environ.get("VIGIORA_API_KEY")
    if not key:
        raise SystemExit("set VIGIORA_API_KEY")
    import aiohttp  # noqa: PLC0415
    import aiomqtt  # noqa: PLC0415

    async with aiohttp.ClientSession() as session, aiomqtt.Client(args.broker, args.port) as mqtt:
        client = vt.ThingsClient(vt.AiohttpTransport(session), args.url, key)
        server = await client.server_info()
        if server is None or not server.has("things"):
            raise SystemExit("this Vigiora server has no Things model (API 1.18.0+)")

        class Pub:
            async def publish(self, topic: str, payload: str, retain: bool = False) -> None:
                await mqtt.publish(topic, payload, retain=retain)

            async def subscribe(self, topic: str) -> None:
                await mqtt.subscribe(topic)

        bridge = Bridge(client, Pub(), node_id=server.id, discovery_prefix=args.discovery_prefix)

        async def commands() -> None:
            async for msg in mqtt.messages:
                await bridge.handle_message(str(msg.topic), msg.payload.decode())

        await asyncio.gather(bridge.run(), commands())


if __name__ == "__main__":  # pragma: no cover
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_main())
