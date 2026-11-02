"""Things mode (API 1.17.0/1.18.0 servers): entities from the mapping table."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.vigiora.const import DOMAIN
from custom_components.vigiora.vigiora_things import ThingEvent
from homeassistant.components.camera import async_get_stream_source
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .conftest import URL, WRITE_SCOPES, add_api


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _event(typ: str, subject: str, data: dict, v1: str, ev_id: str) -> ThingEvent:
    return ThingEvent.from_dict(
        {
            "specversion": "1.0",
            "id": ev_id,
            "type": typ,
            "subject": subject,
            "source": f"vigiora://x/things/{subject}",
            "time": "2099-01-01T00:00:00Z",
            "data": data,
            "vigioracapability": typ.split(".")[1],
            "vigioracatalogversion": "1.0",
            "vigioraeventtype": v1,
        }
    )


async def test_things_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Entities come from the Things table; motion is not polled."""
    add_api(aioclient_mock, things=True)
    await _setup(hass, config_entry)
    runtime = config_entry.runtime_data
    assert runtime.coordinator.caps.things_mode
    assert runtime.motion is None  # no 10 s motion polling
    assert not any("motion/latest" in str(call[1]) for call in aioclient_mock.mock_calls)

    assert hass.states.get("binary_sensor.entrance_motion").state == STATE_OFF
    assert hass.states.get("binary_sensor.entrance_person_in_view").state == STATE_OFF
    assert hass.states.get("binary_sensor.entrance_online").state == STATE_ON
    assert hass.states.get("binary_sensor.garden_online").state == STATE_OFF
    assert hass.states.get("sensor.vigiora_site_unacknowledged_alerts").state == "746"
    assert hass.states.get("sensor.vigiora_site_shop_occupancy").state == "3"
    assert hass.states.get("sensor.vigiora_site_shop_visitors_today").state == "42"
    # Read-only key: no switches or selects.
    assert hass.states.get("switch.entrance_recording") is None

    # Unique ids are the 0.1.0 ones.
    ent = er.async_get(hass).async_get("binary_sensor.entrance_motion")
    assert ent.unique_id == f"{config_entry.entry_id}_camera_1_motion"
    # The server's identity from GET /server.
    device = dr.async_get(hass).async_get_device(identifiers={(DOMAIN, config_entry.entry_id)})
    assert device.sw_version == "1.7.0"
    assert device.serial_number == "5f1c0ffee5f1c0ffee5f1c0ffee5f1c0"


async def test_things_events_push_state(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Motion and presence are pushed; Guardian events need no extra GET."""
    add_api(aioclient_mock, things=True)
    await _setup(hass, config_entry)
    stream = config_entry.runtime_data.events

    stream._on_thing_event(
        _event(
            "vigiora.motion.started",
            "camera:1",
            {"started_at": "2099-01-01T00:00:00Z"},
            "motion.started",
            "01JC0000000000000000000001",
        )
    )
    stream._on_thing_event(
        _event(
            "vigiora.presence.changed",
            "camera:1",
            {"label": "person", "present": True},
            "presence.changed",
            "01JC0000000000000000000002",
        )
    )
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.entrance_motion").state == STATE_ON
    assert hass.states.get("binary_sensor.entrance_person_in_view").state == STATE_ON

    stream._on_thing_event(
        _event("vigiora.motion.ended", "camera:1", {}, "motion.ended", "01JC0000000000000000000003")
    )
    stream._on_thing_event(
        _event(
            "vigiora.device.offline",
            "camera:1",
            {"status": "idle"},
            "camera.offline",
            "01JC0000000000000000000004",
        )
    )
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.entrance_motion").state == STATE_OFF
    assert hass.states.get("binary_sensor.entrance_online").state == STATE_OFF

    # Guardian: presence in the event, signed snapshot link.
    calls_before = len(aioclient_mock.mock_calls)
    stream._on_thing_event(
        _event(
            "vigiora.ai_guardian.observed",
            "camera:1",
            {
                "entry_id": 872,
                "description": "A courier at the door.",
                "observed_at": "2099-01-01T00:00:00Z",
                "has_frame": True,
                "person_present": True,
                "person_present_seconds": 6.5,
                "detector_checked": True,
                "detections": [{"label": "person", "count": 1, "max_confidence": 0.9, "tracks": 1}],
                "snapshot_url": "/api/v1/media/tok.sig/image",
                "snapshot_url_expires_at": "2099-01-01T00:15:00Z",
            },
            "guardian.event",
            "01JC0000000000000000000005",
        )
    )
    await hass.async_block_till_done()
    assert len(aioclient_mock.mock_calls) == calls_before  # no GET /guardian/entries/872
    state = hass.states.get("event.entrance_guardian")
    assert state.attributes["event_type"] == "scene_description"
    assert state.attributes["person_present"] is True
    assert state.attributes["detector_checked"] is True
    assert state.attributes["snapshot_url"] == f"{URL}/api/v1/media/tok.sig/image"

    # An alert with a signed snapshot link.
    stream._on_thing_event(
        _event(
            "vigiora.alert.created",
            "camera:1",
            {
                "alert_id": 9,
                "label": "person",
                "is_alert": True,
                "severity": "high",
                "detected_at": "2099-01-01T00:00:00Z",
                "has_snapshot": True,
                "snapshot_url": "/api/v1/media/a.b/image",
            },
            "alert.created",
            "01JC0000000000000000000006",
        )
    )
    await hass.async_block_till_done()
    state = hass.states.get("event.entrance_guardian")
    assert state.attributes["event_type"] == "alert"
    assert state.attributes["snapshot_url"] == f"{URL}/api/v1/media/a.b/image"


async def test_things_switch_and_select_commands(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Switches and selects run Things commands."""
    add_api(aioclient_mock, things=True, scopes=WRITE_SCOPES)
    aioclient_mock.post(
        f"{URL}/api/v1/things/camera:1/commands/recording.turn_off",
        json={
            "thing_id": "camera:1",
            "command": "recording.turn_off",
            "operation_id": "updateCamera",
            "result": {"id": 1},
        },
    )
    aioclient_mock.post(
        f"{URL}/api/v1/things/room:7/commands/ai_mode.set",
        json={
            "thing_id": "room:7",
            "command": "ai_mode.set",
            "operation_id": "updateRoom",
            "result": {"id": 7},
        },
    )
    await _setup(hass, config_entry)
    assert hass.states.get("switch.entrance_recording").state == STATE_ON
    assert hass.states.get("switch.garden_recording").state == STATE_OFF
    await hass.services.async_call(
        "switch", "turn_off", {"entity_id": "switch.entrance_recording"}, blocking=True
    )
    sent = [c for c in aioclient_mock.mock_calls if "recording.turn_off" in str(c[1])]
    assert len(sent) == 1 and sent[0][2] == {}
    assert hass.states.get("select.home_ai_mode").state == "both"
    await hass.services.async_call(
        "select",
        "select_option",
        {"entity_id": "select.home_ai_mode", "option": "guardian"},
        blocking=True,
    )
    sent = [c for c in aioclient_mock.mock_calls if "ai_mode.set" in str(c[1])]
    assert sent[0][2] == {"mode": "guardian"}


async def test_things_command_respects_hitl(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Human in the loop: a confirm-class command is refused without being sent."""
    add_api(aioclient_mock, things=True, scopes=WRITE_SCOPES, hitl=True)
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "switch", "turn_off", {"entity_id": "switch.entrance_recording"}, blocking=True
        )
    assert not any("/commands/" in str(c[1]) for c in aioclient_mock.mock_calls)


async def test_live_stream_url(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """The camera streams through a signed HLS link, minted on demand."""
    add_api(aioclient_mock, things=True)
    aioclient_mock.post(
        f"{URL}/api/v1/cameras/1/actions/stream-url",
        json={
            "camera_id": 1,
            "hls_url": "/api/v1/media/abc.def/stream.m3u8",
            "rtsp_url": None,
            "webrtc": None,
            "quality": "main",
            "format": "mpegts",
            "expires_at": "2099-01-01T00:10:00Z",
            "ttl_seconds": 600,
        },
    )
    await _setup(hass, config_entry)
    source = await async_get_stream_source(hass, "camera.entrance")
    assert source == f"{URL}/api/v1/media/abc.def/stream.m3u8"
    assert "vgk_" not in source
    # Cached while valid.
    assert await async_get_stream_source(hass, "camera.entrance") == source
    assert len([c for c in aioclient_mock.mock_calls if "stream-url" in str(c[1])]) == 1


async def test_old_server_has_no_stream(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """A server before API 1.17.0: snapshot only, motion polled, v1 entities."""
    add_api(aioclient_mock)
    await _setup(hass, config_entry)
    runtime = config_entry.runtime_data
    assert runtime.server is None
    assert not runtime.coordinator.caps.things_mode
    assert runtime.motion is not None
    assert await async_get_stream_source(hass, "camera.entrance") is None


async def test_speak_through_camera(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """vigiora.speak with a camera uses the camera's own speaker; 404 falls back to its room."""
    add_api(aioclient_mock, things=True, scopes=WRITE_SCOPES)
    aioclient_mock.post(
        f"{URL}/api/v1/cameras/1/actions/speak", json={"ok": True, "camera_id": 1, "output_id": 3}
    )
    aioclient_mock.post(
        f"{URL}/api/v1/cameras/2/actions/speak",
        status=HTTPStatus.NOT_FOUND,
        json={"error": {"code": "not_found", "message": "no speaker", "details": {}}},
    )
    await _setup(hass, config_entry)
    await hass.services.async_call(
        DOMAIN, "speak", {"camera_id": 1, "text": "Hello", "volume": 1.5}, blocking=True
    )
    sent = [c for c in aioclient_mock.mock_calls if "/cameras/1/actions/speak" in str(c[1])]
    assert sent[0][2] == {"text": "Hello", "volume": 1.0}
    # Camera 2 has no speaker and is in no room.
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "speak", {"camera_id": 2, "text": "Hi"}, blocking=True
        )


async def test_alert_count_from_summary(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """A viewer's key gets the alert count from /alerts/summary, not by paging."""
    add_api(aioclient_mock, things=True, role="viewer")
    await _setup(hass, config_entry)
    assert config_entry.runtime_data.coordinator.data.unacknowledged_alerts == 746
    assert not any(str(c[1]).endswith("/api/v1/alerts") for c in aioclient_mock.mock_calls)


THINGS_SSE = (
    b"retry: 1000\n\n"
    b": heartbeat\n\n"
    b"id: 01JC0000000000000000000010\n"
    b"event: vigiora.motion.started\n"
    b'data: {"specversion":"1.0","id":"01JC0000000000000000000010","type":"vigiora.motion.started",'
    b'"subject":"camera:1","source":"vigiora://s/things/camera:1","time":"2099-01-01T00:00:00Z",'
    b'"datacontenttype":"application/json","dataschema":"vigiora:things:1.0:vigiora.motion.started",'
    b'"data":{"started_at":"2099-01-01T00:00:00Z"},"vigioracapability":"motion",'
    b'"vigioracatalogversion":"1.0","vigioraeventtype":"motion.started"}\n'
    b"\n"
)


async def test_things_stream_loop(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """The Things stream (format=things) pushes motion and resumes with Last-Event-ID."""
    from unittest.mock import patch

    add_api(aioclient_mock, things=True)
    aioclient_mock.get(f"{URL}/api/v1/events/stream", content=THINGS_SSE)
    await _setup(hass, config_entry)
    stream = config_entry.runtime_data.events
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 2 and stream._subscription is not None:
            stream._subscription.stop()

    with patch("custom_components.vigiora.coordinator.asyncio.sleep", fake_sleep):
        await stream._run_things()
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.entrance_motion").state == STATE_ON
    calls = [c for c in aioclient_mock.mock_calls if c[1].path == "/api/v1/events/stream"]
    assert len(calls) == 2
    assert "format=things" in str(calls[0][1]) and "motion.started" in str(calls[0][1])
    assert calls[1][3]["Last-Event-ID"] == "01JC0000000000000000000010"


async def test_diagnostics_things(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Diagnostics serialise in things mode and never hold the key."""
    import json

    from custom_components.vigiora.diagnostics import async_get_config_entry_diagnostics

    add_api(aioclient_mock, things=True)
    await _setup(hass, config_entry)
    diag = await async_get_config_entry_diagnostics(hass, config_entry)
    text = json.dumps(diag)
    assert "vgk_" not in text
    assert diag["things_mode"] is True
    assert "camera:1" in diag["things"]
    assert "things" in diag["server"]["features"]
