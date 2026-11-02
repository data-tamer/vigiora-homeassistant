"""Switches: recording and AI Guardian per camera (keys with cameras:write)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VigioraConfigEntry, things_map
from .api import VigioraClient, VigioraError
from .coordinator import VigioraCoordinator
from .entity import VigioraCameraEntity, VigioraThingEntity, thing_allowed
from .errors import to_ha_error
from .vigiora_things import plan

PARALLEL_UPDATES = 1


@dataclass(frozen=True, kw_only=True)
class VigioraSwitchDescription(SwitchEntityDescription):
    """A camera switch."""

    field: str
    set_fn: Callable[[VigioraClient, int, bool], Awaitable[dict[str, Any]]]


SWITCHES: tuple[VigioraSwitchDescription, ...] = (
    VigioraSwitchDescription(
        key="recording",
        translation_key="recording",
        entity_category=EntityCategory.CONFIG,
        field="recording_enabled",
        set_fn=lambda c, cid, on: c.set_recording(cid, on),
    ),
    VigioraSwitchDescription(
        key="ai_guardian",
        translation_key="ai_guardian",
        entity_category=EntityCategory.CONFIG,
        field="ai_enabled",
        set_fn=lambda c, cid, on: c.set_ai(cid, on),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VigioraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add the switches when the key may change cameras."""
    coordinator = entry.runtime_data.coordinator
    if coordinator.caps.things_mode:
        async_add_entities(
            VigioraThingSwitch(coordinator, p.thing_id, p.rule)
            for p in plan(
                coordinator.data.things.values(),
                things_map.rules("switch"),
                allow=thing_allowed(coordinator),
            )
        )
        return
    if not coordinator.caps.can_write_cameras:
        return
    async_add_entities(
        VigioraSwitch(coordinator, cid, desc)
        for cid in coordinator.data.cameras
        if coordinator.camera_selected(cid)
        for desc in SWITCHES
    )


class VigioraSwitch(VigioraCameraEntity, SwitchEntity):
    """Recording or AI on a camera."""

    entity_description: VigioraSwitchDescription

    def __init__(
        self, coordinator: VigioraCoordinator, camera_id: int, desc: VigioraSwitchDescription
    ) -> None:
        """Initialise."""
        super().__init__(coordinator, camera_id, desc.key)
        self.entity_description = desc

    @property
    def is_on(self) -> bool:
        """Current value."""
        return bool(self.camera.get(self.entity_description.field))

    async def _set(self, on: bool) -> None:
        try:
            cam = await self.entity_description.set_fn(self.coordinator.client, self.camera_id, on)
        except VigioraError as err:
            raise to_ha_error(err) from err
        if isinstance(cam, dict) and cam.get("id") == self.camera_id:
            self.coordinator.data.cameras[self.camera_id] = cam
            self.coordinator.async_update_listeners()
        else:
            await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off."""
        await self._set(False)


class VigioraThingSwitch(VigioraThingEntity, SwitchEntity):
    """Recording or AI Guardian: a capability's on state and its commands."""

    @property
    def is_on(self) -> bool | None:
        """The capability's state."""
        value = self.value
        return None if value is None else bool(value)

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Run the table row's on command."""
        if self.rule.on_command:
            await self.run_command(self.rule.on_command)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Run the table row's off command."""
        if self.rule.off_command:
            await self.run_command(self.rule.off_command)
