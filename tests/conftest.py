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
) -> None:
    """Register the mocked API v1 endpoints."""
    scopes = READ_SCOPES if scopes is None else scopes
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
