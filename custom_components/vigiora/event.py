"""Event entities: Guardian alerts and scene descriptions per camera."""

from __future__ import annotations

from typing import Any

from homeassistant.components.event import EventEntity
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import VigioraConfigEntry
from .const import (
    CONF_EVENT_TYPES,
    DEFAULT_EVENT_TYPES,
    EVENT_TYPE_ALERT,
    EVENT_TYPE_SCENE,
    SIGNAL_EVENT,
)
from .coordinator import VigioraCoordinator
from .entity import VigioraCameraEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: VigioraConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Add an event entity per selected camera."""
    coordinator = entry.runtime_data.coordinator
    caps = coordinator.caps
    types = [
        t
        for t in entry.options.get(CONF_EVENT_TYPES, DEFAULT_EVENT_TYPES)
        if (t == EVENT_TYPE_ALERT and caps.can("alerts:read"))
        or (t == EVENT_TYPE_SCENE and caps.can("guardian:read"))
    ]
    if not types:
        return
    async_add_entities(
        VigioraEvent(coordinator, cid, types)
        for cid in coordinator.data.cameras
        if coordinator.camera_selected(cid)
    )


def _signed(base_url: str, data: dict[str, Any]) -> str | None:
    """The event's signed snapshot link (API 1.17.0), absolute; usable without the key."""
    url = data.get("snapshot_url")
    if not isinstance(url, str) or not url:
        return None
    return base_url + url if url.startswith("/") else url


def alert_attributes(base_url: str, event: dict[str, Any]) -> dict[str, Any]:
    """Event attributes of an alert.created event."""
    data = event.get("data") or {}
    label = str(data.get("label") or "")
    rule = label.removeprefix("guardian:") if label.startswith("guardian:") else None
    alert_id = data.get("alert_id")
    return {
        "description": rule or label,
        "severity": data.get("severity"),
        "rule": rule,
        "label": label,
        "confidence": data.get("confidence"),
        "is_alert": data.get("is_alert"),
        "alert_id": alert_id,
        "entry_id": None,
        "source": data.get("source"),
        "occurred_at": data.get("detected_at") or event.get("time"),
        "snapshot_url": _signed(base_url, data)
        or (f"{base_url}/api/v1/alerts/{alert_id}/snapshot" if alert_id else None),
        "snapshot_url_expires_at": data.get("snapshot_url_expires_at"),
        "event_id": event.get("id"),
    }


def scene_attributes(base_url: str, event: dict[str, Any]) -> dict[str, Any]:
    """Event attributes of a guardian.event event."""
    data = event.get("data") or {}
    entry_id = data.get("entry_id")
    has_frame = data.get("has_frame", True)
    return {
        "description": data.get("description"),
        "severity": None,
        "rule": None,
        "alert_id": None,
        "entry_id": entry_id,
        "audio_transcript": data.get("audio_transcript"),
        "person_present_seconds": data.get("person_present_seconds"),
        "occurred_at": data.get("observed_at") or event.get("time"),
        "person_present": data.get("person_present"),
        "detections": data.get("detections"),
        "detector_checked": data.get("detector_checked"),
        "snapshot_url": _signed(base_url, data)
        or (
            f"{base_url}/api/v1/guardian/entries/{entry_id}/frame"
            if entry_id and has_frame
            else None
        ),
        "snapshot_url_expires_at": data.get("snapshot_url_expires_at"),
        "event_id": event.get("id"),
    }


class VigioraEvent(VigioraCameraEntity, EventEntity):
    """Guardian alerts and scene descriptions of one camera.

    On API 1.17.0+ `snapshot_url` is a signed link valid for 15 minutes that
    needs no key (notifications can show it); on older servers it needs the
    API key (Authorization header).
    """

    _attr_translation_key = "guardian"

    def __init__(self, coordinator: VigioraCoordinator, camera_id: int, types: list[str]) -> None:
        """Initialise."""
        super().__init__(coordinator, camera_id, "events")
        self._attr_event_types = types

    async def async_added_to_hass(self) -> None:
        """Subscribe to the entry's events."""
        await super().async_added_to_hass()
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_EVENT.format(self.coordinator.config_entry.entry_id),
                self._handle_event,
            )
        )

    @callback
    def _handle_event(self, event: dict[str, Any]) -> None:
        if event.get("camera_id") != self.camera_id:
            return
        base = self.coordinator.client.url
        if event.get("type") == "alert.created" and EVENT_TYPE_ALERT in self.event_types:
            self._trigger_event(EVENT_TYPE_ALERT, alert_attributes(base, event))
        elif event.get("type") == "guardian.event" and EVENT_TYPE_SCENE in self.event_types:
            self._trigger_event(EVENT_TYPE_SCENE, scene_attributes(base, event))
        else:
            return
        self.async_write_ha_state()
