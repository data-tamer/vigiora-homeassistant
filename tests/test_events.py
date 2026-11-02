"""Event stream (SSE) tests."""

from __future__ import annotations

import asyncio
from http import HTTPStatus
from unittest.mock import patch

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import (
    AiohttpClientMocker,
    mock_stream,
)

from custom_components.vigiora.api import decode_event, parse_sse
from custom_components.vigiora.coordinator import VigioraEventStream
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant

from .conftest import URL, add_api

SSE_BODY = (
    b"retry: 1000\n\n"
    b": heartbeat\n\n"
    b"id: 01JBXK6P3ZQ8R4V2M7N9T5W1YA\n"
    b"event: alert.created\n"
    b'data: {"id":"01JBXK6P3ZQ8R4V2M7N9T5W1YA","type":"alert.created","time":"2099-01-01T00:00:00Z",'
    b'"camera_id":1,"data":{"alert_id":787,"label":"guardian:Person at the entrance","confidence":1,'
    b'"is_alert":true,"severity":"high","detected_at":"2099-01-01T00:00:00Z","external":false,"source":null}}\n'
    b"\n"
)


async def test_parse_sse() -> None:
    """Frames, comments and multi-line data."""
    frames = [f async for f in parse_sse(mock_stream(SSE_BODY + b"event: x\ndata: a\ndata: b\n\n"))]
    assert frames[0].comment is True
    ev = [f for f in frames if not f.comment]
    assert ev[0].event == "alert.created"
    assert ev[0].id == "01JBXK6P3ZQ8R4V2M7N9T5W1YA"
    assert decode_event(ev[0])["data"]["alert_id"] == 787
    assert ev[1].data == "a\nb"


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> VigioraEventStream:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry.runtime_data.events


async def test_alert_event_fires_entity(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """alert.created fires the camera's event entity with attributes."""
    add_api(aioclient_mock)
    stream = await _setup(hass, config_entry)
    stream.handle_event(
        {
            "id": "01JBXK6P3ZQ8R4V2M7N9T5W1YA",
            "type": "alert.created",
            "time": "2099-01-01T00:00:00Z",
            "camera_id": 1,
            "data": {
                "alert_id": 787,
                "label": "guardian:Person at the entrance",
                "is_alert": True,
                "severity": "high",
                "confidence": 1,
                "detected_at": "2099-01-01T00:00:00Z",
            },
        }
    )
    await hass.async_block_till_done()
    state = hass.states.get("event.entrance_guardian")
    assert state.attributes["event_type"] == "alert"
    assert state.attributes["rule"] == "Person at the entrance"
    assert state.attributes["severity"] == "high"
    assert state.attributes["alert_id"] == 787
    assert state.attributes["snapshot_url"] == f"{URL}/api/v1/alerts/787/snapshot"
    # The other camera's entity did not fire.
    assert hass.states.get("event.garden_guardian").attributes.get("event_type") is None
    # A duplicate (replayed) id is ignored.
    before = state.state
    stream.handle_event(
        {"id": "01JBXK6P3ZQ8R4V2M7N9T5W1YA", "type": "alert.created", "camera_id": 1, "data": {}}
    )
    await hass.async_block_till_done()
    assert hass.states.get("event.entrance_guardian").state == before


async def test_detection_not_alert_is_filtered(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """A raw person detection does not fire the event (alerts only) but sets presence."""
    add_api(aioclient_mock)
    stream = await _setup(hass, config_entry)
    stream.handle_event(
        {
            "id": "01JBXK6P3ZQ8R4V2M7N9T5W1YB",
            "type": "alert.created",
            "camera_id": 1,
            "data": {
                "alert_id": 5,
                "label": "person",
                "is_alert": False,
                "detected_at": "2099-01-01T00:00:00Z",
            },
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("event.entrance_guardian").attributes.get("event_type") is None
    assert hass.states.get("binary_sensor.entrance_person_in_view").state == STATE_ON


async def test_guardian_event(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """guardian.event fires a scene description and person presence from the entry."""
    add_api(aioclient_mock)
    stream = await _setup(hass, config_entry)
    stream.handle_event(
        {
            "id": "01JBXK6P3ZQ8R4V2M7N9T5W1YC",
            "type": "guardian.event",
            "camera_id": 1,
            "data": {
                "entry_id": 872,
                "description": "short",
                "observed_at": "2099-01-01T00:00:00Z",
            },
        }
    )
    await hass.async_block_till_done()
    state = hass.states.get("event.entrance_guardian")
    assert state.attributes["event_type"] == "scene_description"
    assert state.attributes["description"] == "A courier leaves a parcel at the door."
    assert state.attributes["entry_id"] == 872
    assert state.attributes["snapshot_url"] == f"{URL}/api/v1/guardian/entries/872/frame"
    assert hass.states.get("binary_sensor.entrance_person_in_view").state == STATE_ON


async def test_camera_offline_event(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """camera.offline turns the online sensor off at once."""
    add_api(aioclient_mock)
    stream = await _setup(hass, config_entry)
    stream.handle_event(
        {
            "id": "01JBXK6P3ZQ8R4V2M7N9T5W1YD",
            "type": "camera.offline",
            "camera_id": 1,
            "data": {"previous": "live", "status": "idle"},
        }
    )
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.entrance_online").state == STATE_OFF


async def test_stream_consumes_and_reconnects(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """The stream loop dispatches events, keeps Last-Event-ID and backs off on errors."""
    add_api(aioclient_mock)
    aioclient_mock.get(f"{URL}/api/v1/events/stream", content=SSE_BODY)
    stream = await _setup(hass, config_entry)

    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 2:
            stream._stopped = True

    with patch("custom_components.vigiora.coordinator.asyncio.sleep", fake_sleep):
        await stream._run()
    await hass.async_block_till_done()
    assert stream.last_event_id == "01JBXK6P3ZQ8R4V2M7N9T5W1YA"
    assert hass.states.get("event.entrance_guardian").attributes["alert_id"] == 787
    # Reconnected after the clean end (1 s retry hint), sending Last-Event-ID.
    assert sleeps == [1, 1]
    stream_calls = [c for c in aioclient_mock.mock_calls if c[1].path == "/api/v1/events/stream"]
    assert len(stream_calls) == 2
    assert stream_calls[1][3]["Last-Event-ID"] == "01JBXK6P3ZQ8R4V2M7N9T5W1YA"
    assert "types" in str(stream_calls[0][1])


async def test_stream_backoff_on_limit(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """429 stream_limit backs off exponentially; 403 falls back to polling."""
    add_api(aioclient_mock)
    aioclient_mock.get(
        f"{URL}/api/v1/events/stream",
        status=HTTPStatus.TOO_MANY_REQUESTS,
        json={"error": {"code": "stream_limit", "message": "3 streams", "details": {}}},
    )
    stream = await _setup(hass, config_entry)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)
        if len(sleeps) >= 4:
            stream._stopped = True

    with patch("custom_components.vigiora.coordinator.asyncio.sleep", fake_sleep):
        await stream._run()
    assert sleeps == [1.0, 2.0, 4.0, 8.0]
    assert stream.last_error == "stream_limit"
    assert not stream.healthy


async def test_fallback_poll_uses_event_log(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """While the stream is down, the coordinator poll reads /api/v1/events."""
    add_api(aioclient_mock)
    stream = await _setup(hass, config_entry)
    aioclient_mock.clear_requests()
    add_api(aioclient_mock)
    await config_entry.runtime_data.coordinator.async_refresh()
    await hass.async_block_till_done()
    assert any(c[1].path == "/api/v1/events" for c in aioclient_mock.mock_calls)
    assert not stream.healthy
    await asyncio.sleep(0)
