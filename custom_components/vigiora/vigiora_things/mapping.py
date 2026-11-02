"""Table-driven mapping of things to a platform's entities.

A platform adapter (Home Assistant, MQTT discovery, …) declares one table of
`Rule`s: which capability becomes which entity type, which state field it
shows and which commands it sends. `plan()` turns things into entity plans.
Nothing here knows any platform: device classes and platforms are strings.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from .models import Thing


@dataclass(frozen=True, slots=True)
class Rule:
    """One row of a mapping table."""

    capability: str
    platform: str  # the adapter's entity type: binary_sensor, sensor, switch, …
    key: str  # stable per thing: part of the entity's unique id
    kinds: tuple[str, ...] = ()  # thing kinds it applies to (empty = any)
    field: str | None = None  # the state field shown
    device_class: str | None = None
    unit: str | None = None
    state_class: str | None = None
    entity_category: str | None = None
    translation_key: str | None = None
    on_command: str | None = None  # switch on
    off_command: str | None = None  # switch off
    set_command: str | None = None  # select / number: command taking the value
    set_param: str | None = None  # the param the value goes in
    options: tuple[str, ...] = ()
    requires_scope: str | None = None  # an extra scope the entity needs (writes)
    events: tuple[str, ...] = ()  # things event types the entity reacts to

    def applies(self, thing: Thing) -> bool:
        """Whether the rule makes an entity for this thing."""
        if self.kinds and thing.kind not in self.kinds:
            return False
        if not thing.has(self.capability):
            return False
        for cmd in (self.on_command, self.off_command, self.set_command):
            if cmd and not thing.offers(cmd):
                return False
        return True

    def value(self, thing: Thing) -> Any:
        """The state the entity shows."""
        return thing.state(self.capability, self.field) if self.field else None


@dataclass(frozen=True, slots=True)
class EntityPlan:
    """One entity to create: a thing and the rule that maps it."""

    thing_id: str
    rule: Rule

    @property
    def unique_suffix(self) -> str:
        """`<thing id with _>_<key>` (stable)."""
        return f"{self.thing_id.replace(':', '_')}_{self.rule.key}"


def plan(
    things: Iterable[Thing],
    table: Iterable[Rule],
    *,
    platform: str | None = None,
    allow: Any = None,
) -> list[EntityPlan]:
    """Entity plans for every (thing, rule) that applies.

    `allow(rule, thing) -> bool` filters further (scopes, user selection).
    """
    rules = [r for r in table if platform is None or r.platform == platform]
    out: list[EntityPlan] = []
    for thing in things:
        for rule in rules:
            if rule.applies(thing) and (allow is None or allow(rule, thing)):
                out.append(EntityPlan(thing.id, rule))
    return out


def rules_for_event(table: Iterable[Rule], event_type: str) -> list[Rule]:
    """The rules whose entities react to a things event type."""
    return [r for r in table if event_type in r.events]
