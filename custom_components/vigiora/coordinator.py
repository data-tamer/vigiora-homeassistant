"""Data coordinators and the live event stream for Vigiora."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
from datetime import datetime
import logging
from typing import TYPE_CHECKING, Any

from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .api import (
    VigioraApiError,
    VigioraAuthError,
    VigioraClient,
    VigioraConnectionError,
    VigioraError,
    decode_event,
    has_scope,
)
from .const import (
    CONF_ALERTS_ONLY,
    CONF_CAMERAS,
    DOMAIN,
    MOTION_MAX_AGE,
    MOTION_SCAN_INTERVAL,
    PERSON_HOLD,
    SCAN_INTERVAL,
    SIGNAL_EVENT,
    SSE_BACKOFF_INITIAL,
    SSE_BACKOFF_MAX,
    STREAM_EVENT_SCOPES,
    STREAM_HEALTHY_WINDOW,
    THINGS_EVENT_SCOPES,
)
from .vigiora_things import (
    Catalog,
    EventSubscription,
    ServerInfo,
    Thing,
    ThingEvent,
    ThingsApiError,
    ThingsAuthError,
    ThingsClient,
    ThingsError,
    apply_event,
)
from .vigiora_things.models import (
    FEATURE_ALERTS_SUMMARY,
    FEATURE_THINGS,
    FEATURE_THINGS_EVENTS,
    KIND_AREA,
)

if TYPE_CHECKING:
    from . import VigioraConfigEntry

_LOGGER = logging.getLogger(__name__)

# camera.online / camera.offline carry the watcher's status; map it to the
# camera status the REST API reports.
_WATCHER_STATUS = {"live": "online", "standby": "standby", "idle": "offline"}
ONLINE_STATUSES = ("online", "standby")
PERSON_LABELS = ("person",)


@dataclass
class VigioraData:
    """Everything polled from the server."""

    cameras: dict[int, dict[str, Any]] = field(default_factory=dict)
    status: dict[str, Any] | None = None
    rooms: dict[int, dict[str, Any]] = field(default_factory=dict)
    occupancy: dict[int, dict[str, Any]] | None = None
    visitors_today: dict[int, int] = field(default_factory=dict)
    unacknowledged_alerts: int | None = None
    unacknowledged_capped: bool = False
    # Things model (API 1.18.0): every thing by id, with live state.
    things: dict[str, Thing] = field(default_factory=dict)


@dataclass
class Capabilities:
    """What the key may do and which optional endpoints answer."""

    scopes: list[str] | None
    role: str
    username: str
    key_id: int | None
    status_readable: bool = False
    rooms_readable: bool = False
    analytics_available: bool = False
    motion_available: bool = False
    # GET /api/v1/server (None: a server older than API 1.17.0).
    server: ServerInfo | None = None

    def has(self, feature: str) -> bool:
        """Whether the server has an optional feature (feature detection)."""
        return self.server is not None and self.server.has(feature)

    @property
    def things_mode(self) -> bool:
        """Entities and events come from the Things model."""
        return self.has(FEATURE_THINGS) and self.has(FEATURE_THINGS_EVENTS)

    def can(self, scope: str) -> bool:
        """Whether the key has the scope."""
        return has_scope(self.scopes, scope)

    @property
    def can_write_cameras(self) -> bool:
        """Switches need cameras:write and the admin or operator role."""
        return self.can("cameras:write") and self.role in ("admin", "operator")


class VigioraCoordinator(DataUpdateCoordinator[VigioraData]):
    """Polls the server every 60 s; the event stream pushes in between."""

    config_entry: VigioraConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: VigioraConfigEntry,
        client: VigioraClient,
        caps: Capabilities,
    ) -> None:
        """Initialise."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {client.url}",
            update_interval=SCAN_INTERVAL,
        )
        self.client = client
        self.caps = caps
        self.person_seen: dict[int, datetime] = {}
        self.events: VigioraEventStream | None = None
        self.things_client: ThingsClient | None = None
        self.catalog: Catalog | None = None

    @property
    def selected_camera_ids(self) -> set[int] | None:
        """Camera ids chosen in the options (None = all)."""
        chosen = self.config_entry.options.get(CONF_CAMERAS)
        if not chosen:
            return None
        return {int(c) for c in chosen}

    def camera_selected(self, camera_id: int | None) -> bool:
        """Whether a camera is exposed."""
        sel = self.selected_camera_ids
        return camera_id is not None and (sel is None or camera_id in sel)

    async def async_probe_capabilities(self) -> None:
        """Find which optional endpoints this key can use (first refresh)."""
        caps = self.caps
        caps.status_readable = caps.can("settings:read") and caps.role in ("admin", "operator")
        caps.rooms_readable = caps.can("settings:read")
        if caps.can("analytics:read"):
            try:
                await self.client.get_occupancy()
                caps.analytics_available = True
            except VigioraAuthError:
                raise
            except VigioraError as err:
                _LOGGER.debug("Analytics not available: %s", err)
        if caps.can("alerts:read") and not caps.things_mode:
            # Motion levels are polled only without motion events.
            try:
                await self.client.get_motion_latest()
                caps.motion_available = True
            except VigioraAuthError:
                raise
            except VigioraError as err:
                _LOGGER.debug("Motion levels not available: %s", err)

    async def _async_update_data(self) -> VigioraData:
        client, caps = self.client, self.caps
        data = VigioraData()
        try:
            data.cameras = {int(c["id"]): c for c in await client.list_cameras()}
            if caps.status_readable:
                try:
                    data.status = await client.get_system_status()
                    data.unacknowledged_alerts = data.status.get("unacknowledged_alerts")
                except VigioraApiError as err:
                    if err.status not in (403, 404, 503):
                        raise
                    caps.status_readable = False
            if caps.has(FEATURE_ALERTS_SUMMARY) and caps.can("alerts:read"):
                summary = await client.get_alert_summary()
                data.unacknowledged_alerts = summary.get("unacknowledged")
            if data.unacknowledged_alerts is None and caps.can("alerts:read"):
                (
                    data.unacknowledged_alerts,
                    data.unacknowledged_capped,
                ) = await client.count_unacknowledged_alerts()
            if caps.rooms_readable:
                try:
                    data.rooms = {int(r["id"]): r for r in await client.list_rooms()}
                except VigioraApiError as err:
                    if err.status not in (403, 404, 503):
                        raise
                    caps.rooms_readable = False
            if caps.things_mode and self.things_client is not None:
                await self._update_things(data)
            elif caps.analytics_available:
                await self._update_analytics(data)
        except (VigioraAuthError, ThingsAuthError) as err:
            raise ConfigEntryAuthFailed(str(err)) from err
        except ThingsError as err:
            raise UpdateFailed(str(err)) from err
        except VigioraConnectionError as err:
            raise UpdateFailed(f"Cannot reach the Vigiora server: {err}") from err
        except VigioraError as err:
            raise UpdateFailed(str(err)) from err

        # Keep pushed camera status until the next poll replaces it.
        if self.events is not None and not self.events.healthy:
            await self.events.async_poll_fallback()
        return data

    async def _update_things(self, data: VigioraData) -> None:
        """Things (and the area figures the legacy sensors read) from GET /things."""
        if self.things_client is None:
            return
        things = await self.things_client.things()
        data.things = {t.id: t for t in things}
        areas = [t for t in things if t.kind == KIND_AREA]
        data.occupancy = None
        if areas:
            data.occupancy = {}
            for t in areas:
                area_id = t.object_id or 0
                data.occupancy[area_id] = {
                    "area_id": area_id,
                    "name": t.name,
                    "occupancy": t.state("occupancy", "count"),
                    "capacity": t.state("occupancy", "capacity"),
                    "free": t.state("occupancy", "free"),
                }
                visitors = t.state("counter", "in_today")
                if isinstance(visitors, int):
                    data.visitors_today[area_id] = visitors
            self.caps.analytics_available = True

    def thing(self, thing_id: str) -> Thing | None:
        """A thing by id (things mode)."""
        return self.data.things.get(thing_id) if self.data else None

    @callback
    def async_apply_thing_event(self, event: ThingEvent) -> None:
        """Apply a pushed state change (motion, presence, online, recording)."""
        if self.data is None:
            return
        if apply_event(self.data.things, event) is not None:
            self.async_update_listeners()

    async def async_run_command(
        self, thing_id: str, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """Run a Things command, refusing confirm-class ones while HITL is on."""
        if self.things_client is None:
            raise ThingsError("the server has no Things model")
        thing = self.thing(thing_id)
        spec = self.catalog.command(command) if self.catalog else None
        op_id = spec.operations.get(thing.kind) if spec and thing else None
        if op_id:
            await self.client.ensure_no_approval_needed(op_id)
        return await self.things_client.command(thing_id, command, params)

    async def _update_analytics(self, data: VigioraData) -> None:
        try:
            areas = await self.client.get_occupancy()
        except VigioraApiError as err:
            if err.status in (403, 404, 409, 503):
                data.occupancy = None
                return
            raise
        data.occupancy = {int(a["area_id"]): a for a in areas}
        for area_id in data.occupancy:
            try:
                counts = await self.client.get_area_counts_today(area_id)
            except VigioraApiError as err:
                if err.status in (403, 404, 409, 503):
                    continue
                raise
            data.visitors_today[area_id] = int(counts.get("in") or 0)

    # --- pushed state ------------------------------------------------------

    @callback
    def async_set_camera_status(self, camera_id: int, status: str) -> None:
        """Apply a camera.online/offline event."""
        if self.data is None or camera_id not in self.data.cameras:
            return
        self.data.cameras[camera_id] = {**self.data.cameras[camera_id], "status": status}
        self.async_update_listeners()

    @callback
    def async_person_seen(self, camera_id: int, when: datetime) -> None:
        """Record evidence of a person on a camera."""
        prev = self.person_seen.get(camera_id)
        if prev is None or when > prev:
            self.person_seen[camera_id] = when
            self.async_update_listeners()

    def person_in_view(self, camera_id: int) -> bool:
        """A person was seen within PERSON_HOLD."""
        seen = self.person_seen.get(camera_id)
        return seen is not None and dt_util.utcnow() - seen < PERSON_HOLD


class VigioraMotionCoordinator(DataUpdateCoordinator[dict[int, dict[str, Any]]]):
    """Polls /api/v1/motion/latest (motion has no event type)."""

    def __init__(
        self, hass: HomeAssistant, entry: VigioraConfigEntry, client: VigioraClient
    ) -> None:
        """Initialise."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} motion {client.url}",
            update_interval=MOTION_SCAN_INTERVAL,
        )
        self.client = client

    async def _async_update_data(self) -> dict[int, dict[str, Any]]:
        try:
            raw = await self.client.get_motion_latest()
        except VigioraAuthError as err:
            raise ConfigEntryAuthFailed(err.message) from err
        except VigioraError as err:
            raise UpdateFailed(str(err)) from err
        out: dict[int, dict[str, Any]] = {}
        for key, sample in raw.items():
            try:
                out[int(key)] = sample
            except (TypeError, ValueError):
                continue
        return out

    def motion(self, camera_id: int) -> bool | None:
        """Motion now on the camera (None: no sample)."""
        sample = (self.data or {}).get(camera_id)
        if not sample:
            return None
        ts = dt_util.parse_datetime(str(sample.get("timestamp") or ""))
        if ts is None or dt_util.utcnow() - ts > MOTION_MAX_AGE:
            return False
        return bool(sample.get("has_motion"))


def _parse_time(value: Any) -> datetime:
    parsed = dt_util.parse_datetime(str(value or "")) if value else None
    return parsed or dt_util.utcnow()


class VigioraEventStream:
    """Consumes /api/v1/events/stream with reconnect and backoff.

    While the stream is unhealthy, the 60 s coordinator poll calls
    async_poll_fallback(), which reads the event log (or, without
    events:read, the newest alerts and Guardian entries).
    """

    def __init__(self, hass: HomeAssistant, coordinator: VigioraCoordinator) -> None:
        """Initialise."""
        self.hass = hass
        self.coordinator = coordinator
        self.client = coordinator.client
        self.caps = coordinator.caps
        self.entry = coordinator.config_entry
        self.last_event_id: str | None = None
        self.last_activity: datetime | None = None
        self.connected = False
        self.reconnects = 0
        self.last_error: str | None = None
        self.disabled_reason: str | None = None
        self._seen: set[str] = set()
        self._task: asyncio.Task[None] | None = None
        self._stopped = False
        # Fallback polling state.
        self._fallback_since: datetime = dt_util.utcnow()
        self._last_alert_id: int | None = None
        self._last_entry_id: int | None = None
        self._subscription: EventSubscription | None = None

    @property
    def types(self) -> list[str]:
        """Event types to subscribe to (those the key may receive)."""
        scopes = dict(STREAM_EVENT_SCOPES)
        if self.caps.things_mode:
            scopes.update(THINGS_EVENT_SCOPES)
        return [t for t, scope in scopes.items() if self.caps.can(scope)]

    @property
    def healthy(self) -> bool:
        """A frame arrived recently."""
        return (
            self.last_activity is not None
            and dt_util.utcnow() - self.last_activity < STREAM_HEALTHY_WINDOW
        )

    def start(self) -> None:
        """Start the background task."""
        if not self.caps.can("events:read"):
            self.disabled_reason = "missing events:read scope"
            return
        run = self._run_things() if self.caps.things_mode else self._run()
        self._task = self.entry.async_create_background_task(
            self.hass, run, name=f"{DOMAIN} event stream"
        )

    async def stop(self) -> None:
        """Stop the background task."""
        self._stopped = True
        if self._subscription is not None:
            self._subscription.stop()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        backoff = SSE_BACKOFF_INITIAL
        while not self._stopped:
            try:
                async for frame in self.client.stream_events(
                    types=self.types, last_event_id=self.last_event_id
                ):
                    if not self.connected:
                        _LOGGER.debug("Vigiora event stream connected")
                    self.connected = True
                    self.last_activity = dt_util.utcnow()
                    backoff = SSE_BACKOFF_INITIAL
                    if frame.comment:
                        continue
                    if frame.event == "shutdown":
                        break
                    if frame.event in ("overflow", "reset"):
                        # Lost events: the log fills the gap (overflow), or
                        # the id is older than the log (reset): start over.
                        if frame.event == "reset":
                            self.last_event_id = None
                        continue
                    event = decode_event(frame)
                    if event is not None:
                        self.handle_event(event)
                self.connected = False
                await asyncio.sleep(1)  # server's retry hint
            except asyncio.CancelledError:
                raise
            except VigioraAuthError:
                self.connected = False
                self.last_error = "unauthenticated"
                self.entry.async_start_reauth(self.hass)
                return
            except VigioraApiError as err:
                self.connected = False
                self.last_error = err.code
                if err.status == 403:
                    # No events:read after all: rely on polling.
                    self.disabled_reason = err.code
                    _LOGGER.warning("Vigiora event stream refused (%s); polling instead", err.code)
                    return
                retry = err.details.get("retry_after_seconds")
                delay = float(retry) if isinstance(retry, (int, float)) else backoff
                await self._sleep_backoff(delay)
                backoff = min(backoff * 2, SSE_BACKOFF_MAX)
            except VigioraError as err:
                self.connected = False
                self.last_error = type(err).__name__
                _LOGGER.debug("Vigiora event stream dropped: %s", err)
                await self._sleep_backoff(backoff)
                backoff = min(backoff * 2, SSE_BACKOFF_MAX)

    async def _run_things(self) -> None:
        """The Things event stream (vigiora_things.EventSubscription)."""
        client = self.coordinator.things_client
        if client is None:
            return

        def connected(on: bool) -> None:
            self.connected = on
            if on:
                self.last_activity = dt_util.utcnow()

        sub = EventSubscription(
            client,
            self._on_thing_event,
            types=self.types,
            things_format=True,
            on_connected=connected,
            sleep=self._sleep_backoff,
            backoff_initial=SSE_BACKOFF_INITIAL,
            backoff_max=SSE_BACKOFF_MAX,
        )
        sub.last_event_id = self.last_event_id
        self._subscription = sub
        try:
            await sub.run()
        except ThingsAuthError:
            self.connected = False
            self.last_error = "unauthenticated"
            self.entry.async_start_reauth(self.hass)
        except ThingsApiError as err:
            self.connected = False
            self.last_error = err.code
            self.disabled_reason = err.code
            _LOGGER.warning("Vigiora event stream refused (%s); polling instead", err.code)

    @callback
    def _on_thing_event(self, event: ThingEvent) -> None:
        """A Things event: state change, then the v1 handling (entities, refreshes)."""
        self.coordinator.async_apply_thing_event(event)
        self.handle_event(event.to_v1())

    async def _sleep_backoff(self, delay: float) -> None:
        self.reconnects += 1
        await asyncio.sleep(delay)

    # --- dispatch ----------------------------------------------------------

    @callback
    def handle_event(self, event: dict[str, Any]) -> None:
        """Apply one event (from the stream or the fallback poll)."""
        ev_id = event.get("id")
        if isinstance(ev_id, str):
            if ev_id in self._seen:
                return
            self._seen.add(ev_id)
            if len(self._seen) > 2000:
                self._seen = set(sorted(self._seen)[-1000:])
            if self.last_event_id is None or ev_id > self.last_event_id:
                self.last_event_id = ev_id
        ev_type = event.get("type")
        camera_id = event.get("camera_id")
        data = event.get("data") or {}
        coordinator = self.coordinator

        if ev_type in ("camera.online", "camera.offline") and isinstance(camera_id, int):
            status = _WATCHER_STATUS.get(str(data.get("status")))
            if status is None:
                status = "online" if ev_type == "camera.online" else "offline"
            coordinator.async_set_camera_status(camera_id, status)
            return
        if ev_type in ("alert.acknowledged", "alert.unacknowledged", "settings.changed"):
            self.hass.async_create_task(coordinator.async_request_refresh())
            return
        if ev_type == "alert.created":
            label = str(data.get("label") or "")
            if isinstance(camera_id, int) and (label in PERSON_LABELS or label.startswith("face:")):
                coordinator.async_person_seen(camera_id, _parse_time(data.get("detected_at")))
            if data.get("is_alert") or not self.entry.options.get(CONF_ALERTS_ONLY, True):
                self._dispatch(event)
            if data.get("is_alert"):
                self.hass.async_create_task(coordinator.async_request_refresh())
            return
        if ev_type == "guardian.event":
            self.hass.async_create_task(self._handle_guardian(event))

    async def _handle_guardian(self, event: dict[str, Any]) -> None:
        """Fire a guardian.event; enrich it with its entry on old servers.

        API 1.17.0 events carry person_present, detections and
        detector_checked: no extra GET per entry.
        """
        data = dict(event.get("data") or {})
        entry_id = data.get("entry_id")
        camera_id = event.get("camera_id")
        if "person_present" in data:
            if isinstance(camera_id, int) and data.get("person_present") is True:
                self.coordinator.async_person_seen(camera_id, _parse_time(data.get("observed_at")))
        elif isinstance(entry_id, int) and self.caps.can("guardian:read"):
            try:
                entry = await self.client.get_guardian_entry(entry_id)
            except VigioraError as err:
                _LOGGER.debug("Could not read Guardian entry %s: %s", entry_id, err)
            else:
                data["description"] = entry.get("description") or data.get("description")
                data["person_present_seconds"] = entry.get("person_present_seconds")
                data["audio_transcript"] = entry.get("audio_transcript")
                data["has_frame"] = entry.get("has_frame")
                pps = entry.get("person_present_seconds")
                if isinstance(camera_id, int) and isinstance(pps, (int, float)) and pps > 0:
                    self.coordinator.async_person_seen(
                        camera_id, _parse_time(entry.get("observed_at"))
                    )
        self._dispatch({**event, "data": data})

    @callback
    def _dispatch(self, event: dict[str, Any]) -> None:
        async_dispatcher_send(self.hass, SIGNAL_EVENT.format(self.entry.entry_id), event)

    # --- fallback polling --------------------------------------------------

    async def async_poll_fallback(self) -> None:
        """Catch up on events while the stream is down."""
        try:
            if self.caps.can("events:read"):
                await self._poll_event_log()
            else:
                await self._poll_lists()
        except VigioraAuthError:
            raise
        except VigioraError as err:
            _LOGGER.debug("Vigiora fallback poll failed: %s", err)

    async def _poll_event_log(self) -> None:
        params: dict[str, Any] = {
            "limit": 200,
            "types": ",".join(self.types),
            "from": self._fallback_since.isoformat(),
        }
        for _ in range(5):
            page = await self.client.list_events(**params)
            for event in page.get("data") or []:
                self.handle_event(event)
                self._fallback_since = max(self._fallback_since, _parse_time(event.get("time")))
            cursor = page.get("next_cursor")
            if not cursor:
                break
            params["cursor"] = cursor

    async def _poll_lists(self) -> None:
        """Without events:read: synthesise events from the newest rows."""
        if self.caps.can("alerts:read"):
            alerts = await self.client.list_alerts(limit=50)
            newest = max((a["id"] for a in alerts), default=None)
            if self._last_alert_id is not None:
                for alert in sorted(alerts, key=lambda a: a["id"]):
                    if alert["id"] > self._last_alert_id:
                        self.handle_event(
                            {
                                "type": "alert.created",
                                "camera_id": alert.get("camera_id"),
                                "time": alert.get("detected_at"),
                                "data": {
                                    "alert_id": alert["id"],
                                    "label": alert.get("label"),
                                    "confidence": alert.get("confidence"),
                                    "is_alert": alert.get("is_alert"),
                                    "severity": alert.get("severity"),
                                    "detected_at": alert.get("detected_at"),
                                    "source": alert.get("source"),
                                },
                            }
                        )
            if newest is not None:
                self._last_alert_id = max(self._last_alert_id or 0, newest)
        if self.caps.can("guardian:read"):
            entries = await self.client.list_guardian_entries(limit=50)
            newest = max((e["id"] for e in entries), default=None)
            if self._last_entry_id is not None:
                for entry in sorted(entries, key=lambda e: e["id"]):
                    if entry["id"] > self._last_entry_id:
                        self.handle_event(
                            {
                                "type": "guardian.event",
                                "camera_id": entry.get("camera_id"),
                                "time": entry.get("observed_at"),
                                "data": {
                                    "entry_id": entry["id"],
                                    "description": entry.get("description"),
                                    "observed_at": entry.get("observed_at"),
                                },
                            }
                        )
            if newest is not None:
                self._last_entry_id = max(self._last_entry_id or 0, newest)

    def diagnostics(self) -> dict[str, Any]:
        """State for diagnostics (no secrets)."""
        return {
            "connected": self.connected,
            "healthy": self.healthy,
            "last_activity": self.last_activity.isoformat() if self.last_activity else None,
            "last_event_id": self.last_event_id,
            "reconnects": self.reconnects
            + (self._subscription.reconnects if self._subscription else 0),
            "things_format": self.caps.things_mode,
            "last_error": self.last_error,
            "disabled_reason": self.disabled_reason,
            "types": self.types,
        }
