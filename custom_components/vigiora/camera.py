"""Camera entities: snapshots and, on API 1.17.0+, live video.

Snapshots come from /api/v1/cameras/{id}/snapshot. Live video uses a signed,
short-lived HLS link (POST /cameras/{id}/actions/stream-url, feature
"stream_url"): the link carries no API key, and it is renewed before it
expires (Stream.update_source). Older servers: snapshot only.
"""

from __future__ import annotations

from datetime import datetime
import logging

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.util import dt as dt_util

from . import VigioraConfigEntry
from .api import VigioraError
from .const import STREAM_URL_REFRESH_MARGIN, STREAM_URL_TTL
from .coordinator import VigioraCoordinator
from .entity import VigioraCameraEntity, has_video
from .vigiora_things.models import FEATURE_STREAM_URL

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
        self._stream_url: str | None = None
        self._stream_expires: datetime | None = None
        self._unsub_refresh: CALLBACK_TYPE | None = None
        if coordinator.caps.has(FEATURE_STREAM_URL) and coordinator.caps.can("cameras:read"):
            self._attr_supported_features = CameraEntityFeature.STREAM

    async def _mint_stream_url(self) -> str | None:
        """A fresh signed HLS link (absolute), and the renewal timer."""
        client = self.coordinator.client
        try:
            res = await client.stream_url(self.camera_id, STREAM_URL_TTL)
        except VigioraError as err:
            _LOGGER.debug("Live video link for camera %s failed: %s", self.camera_id, err)
            return None
        hls = res.get("hls_url")
        if not isinstance(hls, str) or not hls:
            return None
        self._stream_url = client.url + hls if hls.startswith("/") else hls
        self._stream_expires = dt_util.parse_datetime(str(res.get("expires_at") or ""))
        self._schedule_refresh()
        return self._stream_url

    @callback
    def _schedule_refresh(self) -> None:
        if self._unsub_refresh is not None:
            self._unsub_refresh()
            self._unsub_refresh = None
        if self._stream_expires is None or self.hass is None:
            return
        at = max(self._stream_expires - STREAM_URL_REFRESH_MARGIN, dt_util.utcnow())

        async def _refresh(_now: datetime) -> None:
            self._unsub_refresh = None
            url = await self._mint_stream_url()
            if url and self.stream is not None:
                # The running stream switches to the new link.
                self.stream.update_source(url)

        self._unsub_refresh = async_track_point_in_utc_time(self.hass, _refresh, at)

    async def stream_source(self) -> str | None:
        """The live HLS link (renewed before it expires)."""
        if not self.supported_features & CameraEntityFeature.STREAM:
            return None
        if (
            self._stream_url
            and self._stream_expires
            and self._stream_expires - dt_util.utcnow() > STREAM_URL_REFRESH_MARGIN
        ):
            return self._stream_url
        return await self._mint_stream_url()

    async def async_will_remove_from_hass(self) -> None:
        """Cancel the renewal."""
        if self._unsub_refresh is not None:
            self._unsub_refresh()
            self._unsub_refresh = None
        await super().async_will_remove_from_hass()

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
