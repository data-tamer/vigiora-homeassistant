"""Base entities for Vigiora."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER
from .coordinator import VigioraCoordinator

if TYPE_CHECKING:
    from . import VigioraConfigEntry


def server_device_info(entry: VigioraConfigEntry) -> DeviceInfo:
    """The Vigiora server device."""
    runtime = entry.runtime_data
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer=MANUFACTURER,
        model="Vigiora Server",
        sw_version=runtime.version,
        configuration_url=runtime.client.url,
    )


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
