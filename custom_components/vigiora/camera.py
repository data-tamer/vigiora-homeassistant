"""Camera entities: snapshots from /api/v1/cameras/{id}/snapshot.

API v1 exposes no live stream URL (RTSP/HLS/WebRTC) for third parties, and the
camera's own `stream_url` is the source address with credentials redacted, so
these cameras are snapshot-only.
"""

from __future__ import annotations

import logging

from homeassistant.components.camera import Camera
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VigioraConfigEntry
from .api import VigioraError
from .coordinator import VigioraCoordinator
from .entity import VigioraCameraEntity, has_video

_LOGGER = logging.getLogger(__name__)
PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VigioraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add a camera entity per selected camera."""
    coordinator = entry.runtime_data.coordinator
    async_add_entities(
        VigioraCamera(coordinator, cid)
        for cid in coordinator.data.cameras
        if coordinator.camera_selected(cid) and has_video(coordinator.data.cameras[cid])
    )


class VigioraCamera(VigioraCameraEntity, Camera):
    """A Vigiora camera (snapshot only)."""

    _attr_name = None
    _attr_translation_key = "camera"

    def __init__(self, coordinator: VigioraCoordinator, camera_id: int) -> None:
        """Initialise."""
        VigioraCameraEntity.__init__(self, coordinator, camera_id, "camera")
        Camera.__init__(self)
        self._last_image: bytes | None = None

    @property
    def is_on(self) -> bool:
        """Whether the camera is enabled on the server."""
        return bool(self.camera.get("enabled"))

    @property
    def is_recording(self) -> bool:
        """Recording: switched on and the camera enabled."""
        return bool(self.camera.get("enabled") and self.camera.get("recording_enabled"))

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        """Camera facts (no URLs or credentials)."""
        cam = self.camera
        return {
            "camera_id": self.camera_id,
            "status": cam.get("status"),
            "ai_enabled": cam.get("ai_enabled"),
            "has_ptz": cam.get("has_ptz"),
            "has_speaker": cam.get("has_speaker"),
            "has_audio": cam.get("has_audio"),
            "site_id": cam.get("site_id"),
        }

    async def async_camera_image(
        self, width: int | None = None, height: int | None = None
    ) -> bytes | None:
        """Return a JPEG from the snapshot endpoint."""
        try:
            self._last_image = await self.coordinator.client.get_snapshot(self.camera_id)
        except VigioraError as err:
            _LOGGER.debug("Snapshot of camera %s failed: %s", self.camera_id, err)
            return self._last_image
        return self._last_image
