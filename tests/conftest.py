"""Fixtures for the Vigiora tests: a mocked API v1 server."""

from __future__ import annotations

from collections.abc import Generator
from http import HTTPStatus
from typing import Any
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.vigiora.const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL, DOMAIN

URL = "https://site.vigiora.com"
API_KEY = "vgk_testsecret_DO_NOT_LOG"

READ_SCOPES = [
    "cameras:read",
    "alerts:read",
    "analytics:read",
    "guardian:read",
    "settings:read",
    "events:read",
]
WRITE_SCOPES = [*READ_SCOPES, "cameras:write", "cameras:control", "alerts:write", "guardian:write"]

CONFIRM_OPS = [
    "updateCamera",
    "enableCameraAI",
    "disableCameraAI",
    "movePTZ",
    "gotoPTZPreset",
    "speakInRoom",
    "updateRoom",
]


def camera(cid: int, name: str, **kw: Any) -> dict[str, Any]:
    """A Camera object as the API returns it (subset of fields)."""
    return {
        "id": cid,
        "name": name,
        "enabled": True,
        "status": "online",
        "source_type": "rtsp",
        "connection_type": "local",
        "stream_url": "rtsp://REDACTED:REDACTED@192.168.1.20:554/stream1",
        "recording_enabled": True,
        "ai_enabled": False,
        "has_ptz": False,
        "has_speaker": False,
        "has_audio": False,
        "site_id": None,
        "onvif_url": None,
        "hardware_id": "aa:bb:cc:dd:ee:ff",
        "lat": 13.7,
        "lon": 100.5,
        **kw,
    }


CAMERAS = [
    camera(1, "Entrance", has_ptz=True, has_speaker=True),
    camera(2, "Garden", status="offline", recording_enabled=False),
]
ROOMS = [
    {
        "id": 7,
        "name": "Home",
        "ai_mode": "both",
        "cameras": [{"camera_id": 1, "name": "Entrance", "role": "both", "is_primary": True}],
    }
]


def add_api(
    aioclient_mock: AiohttpClientMocker,
    *,
    scopes: list[str] | None = None,
    role: str = "admin",
    hitl: bool = False,
    me_status: int = HTTPStatus.OK,
    analytics: bool = True,
    things: bool = False,
) -> None:
    """Register the mocked API v1 endpoints.

    things=False is a server older than API 1.17.0 (GET /server is 404);
    things=True a 1.18.0 server with the Things model.
    """
    scopes = READ_SCOPES if scopes is None else scopes
    if things:
        add_things_api(aioclient_mock)
    else:
        aioclient_mock.get(
            f"{URL}/api/v1/server",
            status=HTTPStatus.NOT_FOUND,
            json={"error": {"code": "not_found", "message": "No such endpoint", "details": {}}},
        )
    aioclient_mock.get(
        f"{URL}/api/v1/me",
        status=me_status,
        json=(
            {
                "user": {"id": 3, "username": "adrian", "role": role},
                "auth": {"type": "api_key", "key_id": 9, "scoped": True, "scopes": scopes},
            }
            if me_status == HTTPStatus.OK
            else {"error": {"code": "unauthenticated", "message": "bad key", "details": {}}}
        ),
    )
    aioclient_mock.get(
        f"{URL}/api/v1/agent-policy",
        json={
            "human_in_the_loop": hitl,
            "source": "server",
            "override": "inherit",
            "server_human_in_the_loop": hitl,
            "confirm_operations": CONFIRM_OPS,
            "note": "",
        },
    )
    aioclient_mock.get(
        f"{URL}/api/v1/system/status",
        json={
            "version": "1.6.13",
            "cpu_count": 4,
            "load_average": [0.1, 0.1, 0.1],
            "disks": [],
            "cameras_enabled": 2,
            "recordings": {},
            "unacknowledged_alerts": 5,
        },
    )
    aioclient_mock.get(f"{URL}/api/v1/cameras", json={"data": CAMERAS, "next_cursor": None})
    aioclient_mock.get(f"{URL}/api/v1/rooms", json={"data": ROOMS, "next_cursor": None})
    if analytics:
        aioclient_mock.get(
            f"{URL}/api/v1/analytics/occupancy",
            json={
                "areas": [
                    {
                        "area_id": 1,
                        "name": "Shop",
                        "occupancy": 3,
                        "capacity": 10,
                        "free": 7,
                        "source": "lines",
                        "since": "2026-11-02T00:00:00Z",
                    }
                ]
            },
        )
        aioclient_mock.get(
            f"{URL}/api/v1/analytics/counts",
            json={
                "from": "",
                "to": "",
                "tz": "",
                "class": "person",
                "in": 42,
                "out": 39,
                "covered_pct": 100,
                "step": None,
                "series": None,
                "by_class": {},
            },
        )
    else:
        aioclient_mock.get(
            f"{URL}/api/v1/analytics/occupancy",
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            json={"error": {"code": "service_unavailable", "message": "no add-on", "details": {}}},
        )
    aioclient_mock.get(
        f"{URL}/api/v1/motion/latest",
        json={
            "motion": {
                "1": {
                    "has_motion": True,
                    "monitor_id": 1,
                    "motion_percent": 4.8,
                    "timestamp": "2099-01-01T00:00:00Z",
                }
            }
        },
    )
    aioclient_mock.get(f"{URL}/api/v1/alerts", json={"data": [], "next_cursor": None})
    aioclient_mock.get(f"{URL}/api/v1/guardian/entries", json={"data": [], "next_cursor": None})
    aioclient_mock.get(f"{URL}/api/v1/events", json={"data": [], "next_cursor": None})
    aioclient_mock.get(
        f"{URL}/api/v1/guardian/entries/872",
        json={
            "id": 872,
            "camera_id": 1,
            "description": "A courier leaves a parcel at the door.",
            "person_present_seconds": 11.7,
            "audio_transcript": None,
            "has_frame": True,
            "observed_at": "2099-01-01T00:00:00Z",
        },
    )
    aioclient_mock.get(f"{URL}/api/v1/cameras/1/snapshot", content=b"\xff\xd8jpeg")


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable custom integrations."""


@pytest.fixture(autouse=True)
def no_event_stream() -> Generator[None]:
    """Do not open the SSE stream in entity tests (tested separately)."""
    with patch("custom_components.vigiora.coordinator.VigioraEventStream.start"):
        yield


@pytest.fixture
def config_entry() -> MockConfigEntry:
    """A Vigiora config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Vigiora site",
        unique_id=URL,
        data={CONF_URL: URL, CONF_API_KEY: API_KEY, CONF_VERIFY_SSL: True},
    )


FEATURES = [
    "alerts_summary",
    "camera_speak",
    "guardian_presence",
    "motion_events",
    "presence_events",
    "server_identity",
    "signed_media_urls",
    "stream_url",
    "things",
    "things_event_format",
]
SERVER = {
    "id": "5f1c0ffee5f1c0ffee5f1c0ffee5f1c0",
    "name": "base01",
    "site_address": "https://base01.vigiora.com",
    "version": "1.7.0",
    "edition": "self_hosted",
    "install": "native",
    "timezone": "Asia/Bangkok",
    "api_version": "1.18.0",
    "features": FEATURES,
    "time": "2026-11-02T00:00:00Z",
}


def cap(
    name: str,
    state: dict[str, Any] | None = None,
    commands: list[str] | None = None,
    events: list[str] | None = None,
) -> dict[str, Any]:
    """A thing capability."""
    return {
        "name": name,
        "version": "1.0",
        "state": state or {},
        "commands": commands or [],
        "events": events or [],
    }


def camera_thing(
    cid: int,
    name: str,
    *,
    online: bool = True,
    recording: bool = True,
    ptz: bool = False,
    speak: bool = False,
) -> dict[str, Any]:
    """A camera thing."""
    caps = [
        cap("device", {"online": online}),
        cap(
            "alert",
            {"unacknowledged": 2, "by_severity": {"high": 1, "normal": 1}},
            ["alert.acknowledge"],
        ),
        cap("recording", {"on": recording}, ["recording.turn_on", "recording.turn_off"]),
        cap("video_stream", {"available": True}, ["video_stream.get_url"]),
        cap("snapshot", {}, ["snapshot.get_url"]),
        cap(
            "motion",
            {"active": False, "since": None},
            [],
            ["vigiora.motion.started", "vigiora.motion.ended"],
        ),
        cap("presence", {"person": None}, [], ["vigiora.presence.changed"]),
        cap("object_detection"),
        cap("ai_guardian", {"on": False}, ["ai_guardian.turn_on", "ai_guardian.turn_off"]),
        cap("describe", {}, ["describe.run"]),
        cap("detect_objects", {}, ["detect_objects.run"]),
    ]
    if ptz:
        caps.append(cap("ptz", {}, ["ptz.move", "ptz.stop", "ptz.goto_preset"]))
    if speak:
        caps.append(cap("speak", {}, ["speak.say"]))
    return {
        "id": f"camera:{cid}",
        "kind": "camera",
        "name": name,
        "parent_id": "server",
        "site_id": None,
        "room_ids": ["room:7"] if cid == 1 else [],
        "online": online,
        "manufacturer": None,
        "model": None,
        "capabilities": caps,
    }


THINGS = [
    {
        "id": "server",
        "kind": "server",
        "name": "base01",
        "parent_id": None,
        "site_id": None,
        "room_ids": [],
        "online": True,
        "manufacturer": "Vigiora",
        "model": "Vigiora Server",
        "capabilities": [
            cap("device", {"online": True}),
            cap(
                "alert",
                {"unacknowledged": 746, "by_severity": {"normal": 746}},
                ["alert.acknowledge"],
            ),
        ],
    },
    camera_thing(1, "Entrance", ptz=True, speak=True),
    camera_thing(2, "Garden", online=False, recording=False),
    {
        "id": "room:7",
        "kind": "room",
        "name": "Home",
        "parent_id": "server",
        "site_id": None,
        "room_ids": [],
        "online": None,
        "manufacturer": None,
        "model": None,
        "capabilities": [
            cap("ai_mode", {"mode": "both"}, ["ai_mode.set"]),
            cap("speak", {}, ["speak.say"]),
        ],
    },
    {
        "id": "area:1",
        "kind": "area",
        "name": "Shop",
        "parent_id": "server",
        "site_id": None,
        "room_ids": [],
        "online": None,
        "manufacturer": None,
        "model": None,
        "capabilities": [
            cap("occupancy", {"count": 3, "capacity": 10, "free": 7}),
            cap("counter", {"in_today": 42, "out_today": 39}),
        ],
    },
]


def _command(name: str, ops: dict[str, str], scope: str, hitl: str = "none") -> dict[str, Any]:
    return {
        "name": name,
        "description": "",
        "operations": ops,
        "scope": scope,
        "roles": ["admin", "operator"],
        "hitl": hitl,
        "params_schema": {},
    }


CATALOG = {
    "version": "1.0",
    "kinds": ["server", "camera", "microphone", "speaker", "room", "area"],
    "envelope": {},
    "capabilities": [
        {
            "name": "recording",
            "version": "1.0",
            "description": "",
            "kinds": ["camera"],
            "state_schema": {},
            "events": [],
            "commands": [
                _command("turn_on", {"camera": "updateCamera"}, "cameras:write", "confirm"),
                _command("turn_off", {"camera": "updateCamera"}, "cameras:write", "confirm"),
            ],
        },
        {
            "name": "ai_guardian",
            "version": "1.0",
            "description": "",
            "kinds": ["camera"],
            "state_schema": {},
            "events": [],
            "commands": [
                _command("turn_on", {"camera": "enableCameraAI"}, "cameras:write", "confirm"),
                _command("turn_off", {"camera": "disableCameraAI"}, "cameras:write", "confirm"),
            ],
        },
        {
            "name": "ai_mode",
            "version": "1.0",
            "description": "",
            "kinds": ["room"],
            "state_schema": {},
            "events": [],
            "commands": [_command("set", {"room": "updateRoom"}, "cameras:write", "confirm")],
        },
    ],
}


def add_things_api(aioclient_mock: AiohttpClientMocker) -> None:
    """The API 1.17.0/1.18.0 endpoints."""
    aioclient_mock.get(f"{URL}/api/v1/server", json=SERVER)
    aioclient_mock.get(f"{URL}/api/v1/thing-capabilities", json=CATALOG)
    aioclient_mock.get(
        f"{URL}/api/v1/things", json={"data": THINGS, "next_cursor": None, "catalog_version": "1.0"}
    )
    aioclient_mock.get(
        f"{URL}/api/v1/alerts/summary",
        json={
            "unacknowledged": 746,
            "alerts": 12,
            "by_severity": {"normal": 746},
            "cameras": [],
            "latest_at": None,
            "latest_id": None,
        },
    )
