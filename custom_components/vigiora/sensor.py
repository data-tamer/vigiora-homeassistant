"""Server-level sensors: alerts, cameras online, analytics occupancy and visitors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.sensor import (
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VigioraConfigEntry, things_map
from .coordinator import ONLINE_STATUSES, VigioraCoordinator, VigioraData
from .entity import VigioraServerEntity, VigioraThingEntity, thing_allowed
from .vigiora_things import plan

PARALLEL_UPDATES = 0


@dataclass(frozen=True, kw_only=True)
class VigioraSensorDescription(SensorEntityDescription):
    """A server sensor."""

    value_fn: Callable[[VigioraData], Any]
    attrs_fn: Callable[[VigioraData], dict[str, Any]] | None = None


def _cameras_online(data: VigioraData) -> int:
    return sum(1 for c in data.cameras.values() if c.get("status") in ONLINE_STATUSES)


SERVER_SENSORS: tuple[VigioraSensorDescription, ...] = (
    VigioraSensorDescription(
        key="unacknowledged_alerts",
        translation_key="unacknowledged_alerts",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda d: d.unacknowledged_alerts,
        attrs_fn=lambda d: {"capped": d.unacknowledged_capped},
    ),
    VigioraSensorDescription(
        key="cameras_online",
        translation_key="cameras_online",
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=_cameras_online,
        attrs_fn=lambda d: {"cameras_total": len(d.cameras)},
    ),
)

VISITORS_TOTAL = VigioraSensorDescription(
    key="visitors_today",
    translation_key="visitors_today",
    state_class=SensorStateClass.TOTAL,
    value_fn=lambda d: sum(d.visitors_today.values()) if d.visitors_today else 0,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VigioraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the server and analytics sensors."""
    coordinator = entry.runtime_data.coordinator
    caps = coordinator.caps
    entities: list[SensorEntity] = []
    if caps.things_mode:
        # Alert counts and the areas' occupancy and visitors from the table.
        entities.extend(
            VigioraThingSensor(coordinator, p.thing_id, p.rule)
            for p in plan(
                coordinator.data.things.values(),
                things_map.rules("sensor"),
                allow=thing_allowed(coordinator),
            )
        )
        entities.append(VigioraServerSensor(coordinator, SERVER_SENSORS[1]))  # cameras online
        if coordinator.data.occupancy:
            entities.append(VigioraServerSensor(coordinator, VISITORS_TOTAL))
        async_add_entities(entities)
        return
    for desc in SERVER_SENSORS:
        if desc.key == "unacknowledged_alerts" and coordinator.data.unacknowledged_alerts is None:
            continue
        entities.append(VigioraServerSensor(coordinator, desc))
    if caps.analytics_available and coordinator.data.occupancy is not None:
        entities.append(VigioraServerSensor(coordinator, VISITORS_TOTAL))
        for area_id, area in coordinator.data.occupancy.items():
            entities.append(VigioraOccupancySensor(coordinator, area_id, area.get("name")))
            entities.append(VigioraAreaVisitorsSensor(coordinator, area_id, area.get("name")))
    async_add_entities(entities)


class VigioraServerSensor(VigioraServerEntity, SensorEntity):
    """A sensor of the server device."""

    entity_description: VigioraSensorDescription

    def __init__(self, coordinator: VigioraCoordinator, desc: VigioraSensorDescription) -> None:
        """Initialise."""
        super().__init__(coordinator, desc.key)
        self.entity_description = desc

    @property
    def native_value(self) -> Any:
        """The value."""
        return self.entity_description.value_fn(self.coordinator.data)

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Extra facts."""
        fn = self.entity_description.attrs_fn
        return fn(self.coordinator.data) if fn else None


class _AreaSensor(VigioraServerEntity, SensorEntity):
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self, coordinator: VigioraCoordinator, area_id: int, area_name: str | None, key: str
    ) -> None:
        super().__init__(coordinator, f"area_{area_id}_{key}")
        self.area_id = area_id
        self._attr_translation_placeholders = {"area": area_name or str(area_id)}

    @property
    def _area(self) -> dict[str, Any] | None:
        occ = self.coordinator.data.occupancy
        return occ.get(self.area_id) if occ else None

    @property
    def available(self) -> bool:
        return super().available and self._area is not None


class VigioraOccupancySensor(_AreaSensor):
    """Live occupancy of an analytics area."""

    _attr_translation_key = "area_occupancy"

    def __init__(self, coordinator: VigioraCoordinator, area_id: int, name: str | None) -> None:
        """Initialise."""
        super().__init__(coordinator, area_id, name, "occupancy")

    @property
    def native_value(self) -> int | None:
        """People in the area."""
        area = self._area
        return area.get("occupancy") if area else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Capacity and source."""
        area = self._area or {}
        return {
            "area_id": self.area_id,
            "capacity": area.get("capacity"),
            "free": area.get("free"),
            "source": area.get("source"),
            "since": area.get("since"),
        }


class VigioraAreaVisitorsSensor(_AreaSensor):
    """People who came in today through the area's entrance lines."""

    _attr_translation_key = "area_visitors_today"
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(self, coordinator: VigioraCoordinator, area_id: int, name: str | None) -> None:
        """Initialise."""
        super().__init__(coordinator, area_id, name, "visitors_today")

    @property
    def native_value(self) -> int | None:
        """Visitors today."""
        return self.coordinator.data.visitors_today.get(self.area_id)


class VigioraThingSensor(VigioraThingEntity, SensorEntity):
    """A capability's numeric state (alert count, occupancy, visitors today)."""

    def __init__(self, coordinator: VigioraCoordinator, thing_id: str, rule: Any) -> None:
        """Initialise."""
        super().__init__(coordinator, thing_id, rule)
        if rule.state_class:
            self._attr_state_class = SensorStateClass(rule.state_class)
        thing = coordinator.thing(thing_id)
        if thing is not None and thing.kind == "area":
            self._attr_translation_placeholders = {"area": thing.name or str(thing.object_id)}

    @property
    def native_value(self) -> Any:
        """The state field."""
        return self.value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """The rest of the capability's state."""
        thing = self.thing
        if thing is None:
            return None
        cap = thing.capabilities.get(self.rule.capability)
        attrs = {k: v for k, v in (cap.state if cap else {}).items() if k != self.rule.field}
        if thing.kind == "area":
            attrs["area_id"] = thing.object_id
        return attrs or None
