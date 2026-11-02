"""Binary sensors: motion, person in view, online."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.const import EntityCategory
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VigioraConfigEntry, things_map
from .const import PERSON_HOLD
from .coordinator import ONLINE_STATUSES, VigioraCoordinator, VigioraMotionCoordinator
from .entity import (
    VigioraCameraEntity,
    VigioraThingEntity,
    camera_device_info,
    camera_unique_id,
    has_video,
)
from .vigiora_things import plan

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VigioraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the binary sensors of every selected camera."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    caps = coordinator.caps
    if caps.things_mode:
        # Things model: one entity per (thing, table row); state is pushed.
        async_add_entities(
            VigioraThingBinarySensor(coordinator, p.thing_id, p.rule)
            for p in plan(
                coordinator.data.things.values(),
                things_map.rules("binary_sensor"),
                allow=lambda _rule, thing: coordinator.camera_selected(thing.object_id),
            )
        )
        return
    entities: list[BinarySensorEntity] = []
    for cid in coordinator.data.cameras:
        if not coordinator.camera_selected(cid):
            continue
        entities.append(VigioraOnlineSensor(coordinator, cid))
        if caps.can("alerts:read") or caps.can("guardian:read"):
            entities.append(VigioraPersonSensor(coordinator, cid))
        if runtime.motion is not None and has_video(coordinator.data.cameras[cid]):
            entities.append(VigioraMotionSensor(coordinator, runtime.motion, cid))
    async_add_entities(entities)


class VigioraOnlineSensor(VigioraCameraEntity, BinarySensorEntity):
    """Camera health: online/standby vs offline/disabled/unknown."""

    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "online"

    def __init__(self, coordinator: VigioraCoordinator, camera_id: int) -> None:
        """Initialise."""
        super().__init__(coordinator, camera_id, "online")

    @property
    def is_on(self) -> bool:
        """Online."""
        return self.camera.get("status") in ONLINE_STATUSES

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """The raw status."""
        return {"status": self.camera.get("status")}


class VigioraPersonSensor(VigioraCameraEntity, BinarySensorEntity):
    """A person was detected (person/face alert, or Guardian presence) recently."""

    _attr_device_class = BinarySensorDeviceClass.OCCUPANCY
    _attr_translation_key = "person"

    def __init__(self, coordinator: VigioraCoordinator, camera_id: int) -> None:
        """Initialise."""
        super().__init__(coordinator, camera_id, "person")
        self._unsub_expire: CALLBACK_TYPE | None = None

    @property
    def is_on(self) -> bool:
        """Person in view."""
        return self.coordinator.person_in_view(self.camera_id)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """When the last person was seen."""
        seen = self.coordinator.person_seen.get(self.camera_id)
        return {"last_seen": seen.isoformat() if seen else None}

    @callback
    def _handle_coordinator_update(self) -> None:
        self._schedule_expiry()
        super()._handle_coordinator_update()

    @callback
    def _schedule_expiry(self) -> None:
        if self._unsub_expire is not None:
            self._unsub_expire()
            self._unsub_expire = None
        seen = self.coordinator.person_seen.get(self.camera_id)
        if seen is None or not self.coordinator.person_in_view(self.camera_id):
            return

        @callback
        def _expire(_now: datetime) -> None:
            self._unsub_expire = None
            self.async_write_ha_state()

        self._unsub_expire = async_track_point_in_utc_time(self.hass, _expire, seen + PERSON_HOLD)

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the timer."""
        if self._unsub_expire is not None:
            self._unsub_expire()
        await super().async_will_remove_from_hass()


class VigioraMotionSensor(CoordinatorEntity[VigioraMotionCoordinator], BinarySensorEntity):
    """Motion from /api/v1/motion/latest."""

    _attr_device_class = BinarySensorDeviceClass.MOTION
    _attr_has_entity_name = True
    _attr_translation_key = "motion"

    def __init__(
        self,
        coordinator: VigioraCoordinator,
        motion: VigioraMotionCoordinator,
        camera_id: int,
    ) -> None:
        """Initialise."""
        super().__init__(motion)
        self.camera_id = camera_id
        entry = coordinator.config_entry
        self._attr_device_info = camera_device_info(
            entry, camera_id, coordinator.data.cameras.get(camera_id) or {}
        )
        self._attr_unique_id = camera_unique_id(entry.entry_id, camera_id, "motion")

    @property
    def is_on(self) -> bool | None:
        """Motion now."""
        return self.coordinator.motion(self.camera_id)

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """The motion level."""
        sample = (self.coordinator.data or {}).get(self.camera_id) or {}
        return {
            "motion_percent": sample.get("motion_percent"),
            "sampled_at": sample.get("timestamp"),
        }


class VigioraThingBinarySensor(VigioraThingEntity, BinarySensorEntity):
    """Motion, person in view or online, from a thing's capability state.

    Motion and presence are pushed (motion.started/ended, presence.changed):
    nothing is polled for them. A camera no detector reports presence for
    falls back to recent evidence of a person (person alerts, Guardian).
    """

    def __init__(self, coordinator: VigioraCoordinator, thing_id: str, rule: Any) -> None:
        """Initialise."""
        super().__init__(coordinator, thing_id, rule)
        self._attr_device_class = BinarySensorDeviceClass(rule.device_class)

    @property
    def is_on(self) -> bool | None:
        """The capability's state."""
        value = self.value
        if self.rule.capability == "presence" and value is None and self.camera_id is not None:
            return self.coordinator.person_in_view(self.camera_id)
        return None if value is None else bool(value)

    @property
    def extra_state_attributes(self) -> dict[str, object] | None:
        """When motion started."""
        thing = self.thing
        if self.rule.capability == "motion" and thing is not None:
            return {"since": thing.state("motion", "since")}
        return None
