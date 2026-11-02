"""Select: a room's AI mode (live / guardian / both), keys with cameras:write."""

from __future__ import annotations

from homeassistant.components.select import SelectEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import VigioraConfigEntry
from .api import VigioraError
from .const import DOMAIN, MANUFACTURER, ROOM_AI_MODES
from .coordinator import VigioraCoordinator
from .errors import to_ha_error

PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VigioraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add an AI mode select per room."""
    coordinator = entry.runtime_data.coordinator
    if not (coordinator.caps.can_write_cameras and coordinator.caps.rooms_readable):
        return
    async_add_entities(VigioraRoomAIMode(coordinator, rid) for rid in coordinator.data.rooms)


class VigioraRoomAIMode(CoordinatorEntity[VigioraCoordinator], SelectEntity):
    """Which AI runs for a room."""

    _attr_has_entity_name = True
    _attr_translation_key = "ai_mode"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_options = ROOM_AI_MODES

    def __init__(self, coordinator: VigioraCoordinator, room_id: int) -> None:
        """Initialise."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.room_id = room_id
        self._attr_unique_id = f"{entry.entry_id}_room_{room_id}_ai_mode"
        room = coordinator.data.rooms.get(room_id) or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{entry.entry_id}_room_{room_id}")},
            name=room.get("name") or f"Room {room_id}",
            manufacturer=MANUFACTURER,
            model="Room",
            via_device=(DOMAIN, entry.entry_id),
        )

    @property
    def available(self) -> bool:
        """Available while the room exists."""
        return super().available and self.room_id in self.coordinator.data.rooms

    @property
    def current_option(self) -> str | None:
        """The room's ai_mode."""
        mode = (self.coordinator.data.rooms.get(self.room_id) or {}).get("ai_mode")
        return mode if mode in ROOM_AI_MODES else None

    async def async_select_option(self, option: str) -> None:
        """PATCH the room."""
        try:
            room = await self.coordinator.client.set_room_ai_mode(self.room_id, option)
        except VigioraError as err:
            raise to_ha_error(err) from err
        if isinstance(room, dict) and room.get("id") == self.room_id:
            self.coordinator.data.rooms[self.room_id] = room
            self.coordinator.async_update_listeners()
        else:
            await self.coordinator.async_request_refresh()
