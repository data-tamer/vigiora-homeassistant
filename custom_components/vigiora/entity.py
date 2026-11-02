"""Base entities for Vigiora."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .api import VigioraError
from .const import DOMAIN, MANUFACTURER
from .coordinator import VigioraCoordinator
from .errors import to_ha_error
from .things_map import unique_id as things_unique_id
from .vigiora_things import Rule, Thing, ThingsError

if TYPE_CHECKING:
    from . import VigioraConfigEntry


def server_device_info(entry: VigioraConfigEntry) -> DeviceInfo:
    """The Vigiora server device."""
    runtime = entry.runtime_data
    server = runtime.server
    info = DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model="Vigiora Server",
        sw_version=runtime.version,
        configuration_url=runtime.client.url,
    )
    if server is not None:
        # GET /api/v1/server: the server's own identity.
        info["serial_number"] = server.id
        if server.edition:
            info["model"] = f"Vigiora Server ({server.edition.replace('_', '-')})"
    return info


def camera_device_info(
    entry: VigioraConfigEntry, camera_id: int, camera: dict[str, Any]
) -> DeviceInfo:
    """The device of one camera, under the server device."""
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_camera_{camera_id}")},
        name=camera.get("name") or f"Camera {camera_id}",
        manufacturer=MANUFACTURER,
        model="Camera",
        via_device=(DOMAIN, entry.entry_id),
        configuration_url=entry.runtime_data.client.url,
    )


def has_video(camera: dict[str, Any]) -> bool:
    """USB microphones (source_type usb_audio) have no picture."""
    return camera.get("source_type") != "usb_audio"


def camera_unique_id(entry_id: str, camera_id: int, suffix: str) -> str:
    """Unique id of a camera entity; services parse the camera id from it."""
    return f"{entry_id}_camera_{camera_id}_{suffix}"


def parse_camera_unique_id(unique_id: str) -> tuple[str, int] | None:
    """Return (entry_id, camera_id) from a camera entity's unique id."""
    entry_id, sep, rest = unique_id.partition("_camera_")
    if not sep:
        return None
    cam, _, _ = rest.partition("_")
    try:
        return entry_id, int(cam)
    except ValueError:
        return None


class VigioraServerEntity(CoordinatorEntity[VigioraCoordinator]):
    """An entity of the server device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: VigioraCoordinator, key: str) -> None:
        """Initialise."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = server_device_info(entry)


class VigioraCameraEntity(CoordinatorEntity[VigioraCoordinator]):
    """An entity of one camera."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: VigioraCoordinator, camera_id: int, key: str) -> None:
        """Initialise."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.camera_id = camera_id
        self._attr_unique_id = camera_unique_id(entry.entry_id, camera_id, key)
        self._attr_device_info = camera_device_info(entry, camera_id, self.camera)

    @property
    def camera(self) -> dict[str, Any]:
        """The camera's latest data."""
        data = self.coordinator.data
        return (data.cameras.get(self.camera_id) if data else None) or {}

    @property
    def available(self) -> bool:
        """Available while the server answers and the camera exists."""
        return super().available and bool(self.camera)


class VigioraThingEntity(CoordinatorEntity[VigioraCoordinator]):
    """An entity built from the Things mapping table (things_map.HA_TABLE)."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: VigioraCoordinator, thing_id: str, rule: Rule) -> None:
        """Initialise from a thing and its table row."""
        super().__init__(coordinator)
        entry = coordinator.config_entry
        self.thing_id = thing_id
        self.rule = rule
        thing = coordinator.thing(thing_id)
        self._attr_unique_id = things_unique_id(entry.entry_id, thing_id, rule.key)
        self._attr_translation_key = rule.translation_key
        if rule.entity_category:
            self._attr_entity_category = EntityCategory(rule.entity_category)
        self._attr_device_info = thing_device_info(entry, thing)

    @property
    def thing(self) -> Thing | None:
        """The thing's latest data."""
        return self.coordinator.thing(self.thing_id)

    @property
    def camera_id(self) -> int | None:
        """The camera id of a camera or microphone thing."""
        thing = self.thing
        return thing.object_id if thing and thing.kind in ("camera", "microphone") else None

    @property
    def value(self) -> Any:
        """The table row's state field."""
        thing = self.thing
        return self.rule.value(thing) if thing else None

    @property
    def available(self) -> bool:
        """Available while the server answers and the thing exists."""
        return super().available and self.thing is not None

    async def run_command(self, command: str, params: dict[str, Any] | None = None) -> Any:
        """Run a Things command and refresh."""
        try:
            result = await self.coordinator.async_run_command(self.thing_id, command, params)
        except (VigioraError, ThingsError) as err:
            raise to_ha_error(err) from err
        await self.coordinator.async_request_refresh()
        return result


def thing_device_info(entry: VigioraConfigEntry, thing: Thing | None) -> DeviceInfo:
    """The device of a thing: cameras and rooms have their own, the rest the server's."""
    if thing is None or thing.kind in ("server", "area", "speaker"):
        return server_device_info(entry)
    if thing.kind in ("camera", "microphone"):
        return camera_device_info(entry, thing.object_id or 0, {"name": thing.name})
    return DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_room_{thing.object_id}")},
        name=thing.name or f"Room {thing.object_id}",
        manufacturer=MANUFACTURER,
        model="Room",
        via_device=(DOMAIN, entry.entry_id),
    )


def thing_allowed(coordinator: VigioraCoordinator) -> Callable[[Rule, Thing], bool]:
    """The table filter: selected cameras only, and the scope a write needs."""
    caps = coordinator.caps

    def allow(rule: Rule, thing: Thing) -> bool:
        if thing.kind in ("camera", "microphone") and not coordinator.camera_selected(
            thing.object_id
        ):
            return False
        return not rule.requires_scope or (
            caps.can(rule.requires_scope) and caps.role in ("admin", "operator")
        )

    return allow
