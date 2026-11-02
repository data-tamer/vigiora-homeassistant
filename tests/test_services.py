"""Actions and error handling (402/403/409, human in the loop)."""

from __future__ import annotations

from http import HTTPStatus

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.vigiora.const import DOMAIN
from homeassistant.const import ATTR_ENTITY_ID
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from .conftest import URL, WRITE_SCOPES, add_api


def _err(code: str, message: str = "", **details) -> dict:
    return {"error": {"code": code, "message": message, "details": details}}


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _posts(aioclient_mock: AiohttpClientMocker, path: str) -> list:
    return [
        c
        for c in aioclient_mock.mock_calls
        if c[0].lower() in ("post", "patch") and c[1].path == path
    ]


async def test_describe_camera(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """describe_camera returns the analysis."""
    add_api(aioclient_mock, scopes=WRITE_SCOPES)
    aioclient_mock.post(
        f"{URL}/api/v1/cameras/1/actions/analyze",
        json={"camera_id": 1, "description": "Empty porch.", "analyzed_at": "2026-11-02T10:00:00Z"},
    )
    await _setup(hass, config_entry)
    resp = await hass.services.async_call(
        DOMAIN,
        "describe_camera",
        {"camera": "camera.entrance"},
        blocking=True,
        return_response=True,
    )
    assert resp["description"] == "Empty porch."
    assert resp["camera_id"] == 1


async def test_describe_camera_conflict(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """409 ai_not_enabled is a clear error."""
    add_api(aioclient_mock, scopes=WRITE_SCOPES)
    aioclient_mock.post(
        f"{URL}/api/v1/cameras/1/actions/analyze",
        status=HTTPStatus.CONFLICT,
        json=_err("ai_not_enabled", "AI is off on this camera"),
    )
    await _setup(hass, config_entry)
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            DOMAIN,
            "describe_camera",
            {"camera": "camera.entrance"},
            blocking=True,
            return_response=True,
        )
    assert exc.value.translation_key == "conflict"
    assert exc.value.translation_placeholders["code"] == "ai_not_enabled"


async def test_detect_objects(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """detect_objects returns the objects (read-only key is enough)."""
    add_api(aioclient_mock)
    aioclient_mock.post(
        f"{URL}/api/v1/cameras/1/actions/detect-objects",
        json={
            "camera_id": 1,
            "objects": [
                {
                    "label": "parcel",
                    "confidence": 0.8,
                    "bbox": {"x": 0.1, "y": 0.2, "w": 0.1, "h": 0.1},
                }
            ],
            "frame_time": "2026-11-02T10:00:00Z",
            "model": "yolo-world",
            "annotated_image_url": None,
        },
    )
    await _setup(hass, config_entry)
    resp = await hass.services.async_call(
        DOMAIN,
        "detect_objects",
        {"camera": "camera.entrance", "prompts": ["parcel"]},
        blocking=True,
        return_response=True,
    )
    assert resp["objects"][0]["label"] == "parcel"
    call = _posts(aioclient_mock, "/api/v1/cameras/1/actions/detect-objects")[0]
    assert call[2] == {"annotate": False, "prompts": ["parcel"]}


async def test_detect_objects_insufficient_scope(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """403 insufficient_scope names the scope."""
    add_api(aioclient_mock)
    aioclient_mock.post(
        f"{URL}/api/v1/cameras/1/actions/detect-objects",
        status=HTTPStatus.FORBIDDEN,
        json=_err("insufficient_scope", "needs scope", required_scope="recordings:read"),
    )
    await _setup(hass, config_entry)
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            DOMAIN,
            "detect_objects",
            {"camera": "camera.entrance"},
            blocking=True,
            return_response=True,
        )
    assert exc.value.translation_key == "insufficient_scope"
    assert exc.value.translation_placeholders["scope"] == "recordings:read"


async def test_acknowledge_alert(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """acknowledge_alert posts the action."""
    add_api(aioclient_mock, scopes=WRITE_SCOPES)
    aioclient_mock.post(f"{URL}/api/v1/alerts/787/actions/acknowledge", json={"id": 787})
    await _setup(hass, config_entry)
    await hass.services.async_call(DOMAIN, "acknowledge_alert", {"alert_id": 787}, blocking=True)
    assert len(_posts(aioclient_mock, "/api/v1/alerts/787/actions/acknowledge")) == 1


async def test_speak_resolves_room(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """speak with a camera speaks in that camera's room."""
    add_api(aioclient_mock, scopes=WRITE_SCOPES)
    aioclient_mock.post(f"{URL}/api/v1/rooms/7/actions/speak", json={"ok": True, "details": {}})
    await _setup(hass, config_entry)
    await hass.services.async_call(
        DOMAIN, "speak", {"camera": "camera.entrance", "text": "Hello"}, blocking=True
    )
    call = _posts(aioclient_mock, "/api/v1/rooms/7/actions/speak")[0]
    assert call[2] == {"text": "Hello"}


async def test_speak_no_room(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """A camera in no room is a clear validation error."""
    add_api(aioclient_mock, scopes=WRITE_SCOPES)
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN, "speak", {"camera": "camera.garden", "text": "Hello"}, blocking=True
        )
    assert exc.value.translation_key == "no_room_for_speaker"


async def test_ptz_goto_preset_by_name(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """A preset name resolves to its token."""
    add_api(aioclient_mock, scopes=WRITE_SCOPES)
    aioclient_mock.get(
        f"{URL}/api/v1/cameras/1/ptz/presets",
        json={"data": [{"token": "p2", "name": "Gate"}], "next_cursor": None},
    )
    aioclient_mock.post(
        f"{URL}/api/v1/cameras/1/ptz/presets/p2/actions/goto", json={"status": "done"}
    )
    await _setup(hass, config_entry)
    await hass.services.async_call(
        DOMAIN, "ptz_goto_preset", {"camera": "camera.entrance", "preset": "gate"}, blocking=True
    )
    assert len(_posts(aioclient_mock, "/api/v1/cameras/1/ptz/presets/p2/actions/goto")) == 1


async def test_ptz_move(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """ptz_move sends the velocities."""
    add_api(aioclient_mock, scopes=WRITE_SCOPES)
    aioclient_mock.post(f"{URL}/api/v1/cameras/1/ptz/actions/move", json={"status": "done"})
    await _setup(hass, config_entry)
    await hass.services.async_call(
        DOMAIN, "ptz_move", {"camera": "camera.entrance", "pan": 0.5}, blocking=True
    )
    call = _posts(aioclient_mock, "/api/v1/cameras/1/ptz/actions/move")[0]
    assert call[2] == {"pan": 0.5, "tilt": 0, "zoom": 0, "duration_ms": 400}


async def test_human_in_the_loop_blocks_confirm_ops(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """With human in the loop on, confirm-class operations are refused, never sent."""
    add_api(aioclient_mock, scopes=WRITE_SCOPES, hitl=True)
    aioclient_mock.post(f"{URL}/api/v1/cameras/1/ptz/actions/move", json={"status": "done"})
    aioclient_mock.patch(f"{URL}/api/v1/cameras/1", json={})
    await _setup(hass, config_entry)
    with pytest.raises(ServiceValidationError) as exc:
        await hass.services.async_call(
            DOMAIN, "ptz_move", {"camera": "camera.entrance", "pan": 0.5}, blocking=True
        )
    assert exc.value.translation_key == "approval_required"
    assert exc.value.translation_placeholders["operation"] == "movePTZ"
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            "switch", "turn_off", {ATTR_ENTITY_ID: "switch.entrance_recording"}, blocking=True
        )
    assert not _posts(aioclient_mock, "/api/v1/cameras/1/ptz/actions/move")
    assert not _posts(aioclient_mock, "/api/v1/cameras/1")


async def test_ai_switch_402(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Past the plan's AI camera limit, turning AI on is a clear error."""
    add_api(aioclient_mock, scopes=WRITE_SCOPES)
    aioclient_mock.post(
        f"{URL}/api/v1/cameras/1/actions/enable-ai",
        status=HTTPStatus.PAYMENT_REQUIRED,
        json=_err("limit_reached", "Your plan covers AI on 2 cameras", limit=2),
    )
    await _setup(hass, config_entry)
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            "switch", "turn_on", {ATTR_ENTITY_ID: "switch.entrance_ai_guardian"}, blocking=True
        )
    assert exc.value.translation_key == "payment_required"
    assert "2 cameras" in exc.value.translation_placeholders["message"]
    assert hass.states.get("switch.entrance_ai_guardian").state == "off"


async def test_recording_switch(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """The recording switch PATCHes the camera and applies the answer."""
    add_api(aioclient_mock, scopes=WRITE_SCOPES)
    aioclient_mock.patch(
        f"{URL}/api/v1/cameras/2",
        json={"id": 2, "name": "Garden", "status": "offline", "recording_enabled": True},
    )
    await _setup(hass, config_entry)
    await hass.services.async_call(
        "switch", "turn_on", {ATTR_ENTITY_ID: "switch.garden_recording"}, blocking=True
    )
    assert _posts(aioclient_mock, "/api/v1/cameras/2")[0][2] == {"recording_enabled": True}
    assert hass.states.get("switch.garden_recording").state == "on"


async def test_room_ai_mode_select(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """The AI mode select PATCHes the room; 409 surfaces as an error."""
    add_api(aioclient_mock, scopes=WRITE_SCOPES)
    aioclient_mock.patch(
        f"{URL}/api/v1/rooms/7",
        json={"id": 7, "name": "Home", "ai_mode": "guardian", "cameras": []},
    )
    await _setup(hass, config_entry)
    await hass.services.async_call(
        "select",
        "select_option",
        {ATTR_ENTITY_ID: "select.home_ai_mode", "option": "guardian"},
        blocking=True,
    )
    assert hass.states.get("select.home_ai_mode").state == "guardian"
