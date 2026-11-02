"""Typed models of the Vigiora Things model (catalogue 1.0).

Plain dataclasses parsed from the API's JSON; unknown fields are ignored so a
newer server (a newer catalogue minor version) never breaks a client.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CATALOG_MAJOR = "1"

# Thing kinds.
KIND_SERVER = "server"
KIND_CAMERA = "camera"
KIND_MICROPHONE = "microphone"
KIND_SPEAKER = "speaker"
KIND_ROOM = "room"
KIND_AREA = "area"

# Optional server features (GET /api/v1/server features).
FEATURE_THINGS = "things"
FEATURE_THINGS_EVENTS = "things_event_format"
FEATURE_MOTION_EVENTS = "motion_events"
FEATURE_PRESENCE_EVENTS = "presence_events"
FEATURE_STREAM_URL = "stream_url"
FEATURE_SIGNED_MEDIA = "signed_media_urls"
FEATURE_CAMERA_SPEAK = "camera_speak"
FEATURE_ALERTS_SUMMARY = "alerts_summary"
FEATURE_GUARDIAN_PRESENCE = "guardian_presence"


def _str(v: Any) -> str | None:
    return v if isinstance(v, str) else None


def _int(v: Any) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) else None


@dataclass(slots=True)
class ServerInfo:
    """GET /api/v1/server."""

    id: str
    name: str
    version: str
    edition: str
    api_version: str
    timezone: str | None = None
    site_address: str | None = None
    install: str | None = None
    features: frozenset[str] = frozenset()

    def has(self, feature: str) -> bool:
        """Whether the server has an optional feature."""
        return feature in self.features

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ServerInfo:
        """Parse the API object."""
        return cls(
            id=str(d.get("id") or ""),
            name=str(d.get("name") or ""),
            version=str(d.get("version") or ""),
            edition=str(d.get("edition") or ""),
            api_version=str(d.get("api_version") or ""),
            timezone=_str(d.get("timezone")),
            site_address=_str(d.get("site_address")),
            install=_str(d.get("install")),
            features=frozenset(f for f in d.get("features") or [] if isinstance(f, str)),
        )


@dataclass(slots=True)
class Capability:
    """One capability of a thing and its current state."""

    name: str
    version: str
    state: dict[str, Any] = field(default_factory=dict)
    commands: tuple[str, ...] = ()
    events: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Capability:
        """Parse the API object."""
        state = d.get("state")
        return cls(
            name=str(d.get("name") or ""),
            version=str(d.get("version") or ""),
            state=dict(state) if isinstance(state, dict) else {},
            commands=tuple(c for c in d.get("commands") or [] if isinstance(c, str)),
            events=tuple(e for e in d.get("events") or [] if isinstance(e, str)),
        )


@dataclass(slots=True)
class Thing:
    """A device: the server, a camera, a microphone, a speaker, a room, an area."""

    id: str
    kind: str
    name: str
    parent_id: str | None = None
    site_id: int | None = None
    room_ids: tuple[str, ...] = ()
    online: bool | None = None
    manufacturer: str | None = None
    model: str | None = None
    capabilities: dict[str, Capability] = field(default_factory=dict)

    @property
    def object_id(self) -> int | None:
        """The numeric id of the underlying object (camera:12 → 12)."""
        _, _, raw = self.id.partition(":")
        try:
            return int(raw)
        except ValueError:
            return None

    def has(self, capability: str) -> bool:
        """Whether the thing has a capability."""
        return capability in self.capabilities

    def state(self, capability: str, key: str, default: Any = None) -> Any:
        """A capability's state field."""
        cap = self.capabilities.get(capability)
        if cap is None:
            return default
        return cap.state.get(key, default)

    def offers(self, command: str) -> bool:
        """Whether the thing offers `<capability>.<command>`."""
        cap = self.capabilities.get(command.partition(".")[0])
        return cap is not None and command in cap.commands

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Thing:
        """Parse the API object."""
        caps: dict[str, Capability] = {}
        for raw in d.get("capabilities") or []:
            if isinstance(raw, dict):
                cap = Capability.from_dict(raw)
                if cap.name:
                    caps[cap.name] = cap
        online = d.get("online")
        return cls(
            id=str(d.get("id") or ""),
            kind=str(d.get("kind") or ""),
            name=str(d.get("name") or ""),
            parent_id=_str(d.get("parent_id")),
            site_id=_int(d.get("site_id")),
            room_ids=tuple(r for r in d.get("room_ids") or [] if isinstance(r, str)),
            online=online if isinstance(online, bool) else None,
            manufacturer=_str(d.get("manufacturer")),
            model=_str(d.get("model")),
            capabilities=caps,
        )


@dataclass(slots=True)
class ThingEvent:
    """An event in the Things envelope (CloudEvents 1.0 attributes)."""

    id: str
    type: str
    subject: str
    time: str | None
    source: str | None = None
    data: dict[str, Any] = field(default_factory=dict)
    capability: str | None = None
    catalog_version: str | None = None
    event_type: str | None = None  # the API v1 type it came from

    @property
    def name(self) -> str:
        """The event part of the type (vigiora.motion.started → started)."""
        return self.type.rpartition(".")[2]

    @property
    def camera_id(self) -> int | None:
        """The camera (or microphone) id of the subject, if any."""
        kind, _, raw = self.subject.partition(":")
        if kind not in (KIND_CAMERA, KIND_MICROPHONE):
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ThingEvent:
        """Parse the envelope."""
        data = d.get("data")
        return cls(
            id=str(d.get("id") or ""),
            type=str(d.get("type") or ""),
            subject=str(d.get("subject") or ""),
            time=_str(d.get("time")),
            source=_str(d.get("source")),
            data=dict(data) if isinstance(data, dict) else {},
            capability=_str(d.get("vigioracapability")),
            catalog_version=_str(d.get("vigioracatalogversion")),
            event_type=_str(d.get("vigioraeventtype")),
        )

    def to_v1(self) -> dict[str, Any]:
        """The API v1 event this came from ({id, type, time, camera_id, data})."""
        return {
            "id": self.id,
            "type": self.event_type or self.type,
            "time": self.time,
            "camera_id": self.camera_id,
            "data": dict(self.data),
        }


@dataclass(slots=True)
class CommandSpec:
    """A command of the catalogue."""

    name: str
    operations: dict[str, str]
    scope: str
    roles: tuple[str, ...]
    hitl: str
    params_schema: dict[str, Any]


@dataclass(slots=True)
class CapabilitySpec:
    """A capability of the catalogue."""

    name: str
    version: str
    kinds: tuple[str, ...]
    state_schema: dict[str, Any]
    commands: dict[str, CommandSpec]
    events: tuple[str, ...]


@dataclass(slots=True)
class Catalog:
    """GET /api/v1/thing-capabilities."""

    version: str
    kinds: tuple[str, ...]
    capabilities: dict[str, CapabilitySpec]

    def command(self, command: str) -> CommandSpec | None:
        """The spec of `<capability>.<command>`."""
        cap, _, name = command.partition(".")
        spec = self.capabilities.get(cap)
        return spec.commands.get(name) if spec else None

    @property
    def compatible(self) -> bool:
        """Same major version as this client."""
        return self.version.partition(".")[0] == CATALOG_MAJOR

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Catalog:
        """Parse the API object."""
        caps: dict[str, CapabilitySpec] = {}
        for raw in d.get("capabilities") or []:
            if not isinstance(raw, dict):
                continue
            cmds = {}
            for c in raw.get("commands") or []:
                if isinstance(c, dict) and c.get("name"):
                    cmds[str(c["name"])] = CommandSpec(
                        name=str(c["name"]),
                        operations={str(k): str(v) for k, v in (c.get("operations") or {}).items()},
                        scope=str(c.get("scope") or ""),
                        roles=tuple(c.get("roles") or ()),
                        hitl=str(c.get("hitl") or "none"),
                        params_schema=dict(c.get("params_schema") or {}),
                    )
            name = str(raw.get("name") or "")
            caps[name] = CapabilitySpec(
                name=name,
                version=str(raw.get("version") or ""),
                kinds=tuple(raw.get("kinds") or ()),
                state_schema=dict(raw.get("state_schema") or {}),
                commands=cmds,
                events=tuple(e.get("type") for e in raw.get("events") or [] if isinstance(e, dict)),
            )
        return cls(
            version=str(d.get("version") or ""),
            kinds=tuple(d.get("kinds") or ()),
            capabilities=caps,
        )


# How an event changes a capability's state: things type → (capability,
# function(data) → state updates). Platform adapters get live state from it.
def _presence(data: dict[str, Any]) -> dict[str, Any] | None:
    if data.get("label", "person") != "person":
        return None
    return {"person": bool(data.get("present"))}


STATE_REDUCERS: dict[str, tuple[str, Any]] = {
    "vigiora.motion.started": (
        "motion",
        lambda d: {"active": True, "since": d.get("started_at")},
    ),
    "vigiora.motion.ended": ("motion", lambda d: {"active": False, "since": None}),
    "vigiora.presence.changed": ("presence", _presence),
    "vigiora.device.online": ("device", lambda d: {"online": True}),
    "vigiora.device.offline": ("device", lambda d: {"online": False}),
    "vigiora.recording.started": ("recording", lambda d: {"on": True}),
    "vigiora.recording.stopped": ("recording", lambda d: {"on": False}),
}


def apply_event(things: dict[str, Thing], event: ThingEvent) -> Thing | None:
    """Apply an event's state change to its thing; return the thing if changed."""
    rule = STATE_REDUCERS.get(event.type)
    thing = things.get(event.subject)
    if rule is None or thing is None:
        return None
    capability, fn = rule
    cap = thing.capabilities.get(capability)
    if cap is None:
        return None
    updates = fn(event.data)
    if not updates:
        return None
    changed = any(cap.state.get(k) != v for k, v in updates.items())
    cap.state.update(updates)
    if capability == "device" and "online" in updates:
        thing.online = updates["online"]
    return thing if changed else None
