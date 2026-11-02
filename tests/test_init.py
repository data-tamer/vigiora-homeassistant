"""Setup and entity creation tests."""

from __future__ import annotations

from http import HTTPStatus

from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import STATE_OFF, STATE_ON
from homeassistant.core import HomeAssistant

from .conftest import API_KEY, WRITE_SCOPES, add_api


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_read_only_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """A read-only key gets cameras, sensors and events, no switches or selects."""
    add_api(aioclient_mock)
    await _setup(hass, config_entry)
    assert config_entry.state is ConfigEntryState.LOADED

    assert hass.states.get("camera.entrance") is not None
    assert hass.states.get("camera.garden") is not None
    assert hass.states.get("binary_sensor.entrance_online").state == STATE_ON
    assert hass.states.get("binary_sensor.garden_online").state == STATE_OFF
    assert hass.states.get("binary_sensor.entrance_motion").state == STATE_ON
    assert hass.states.get("binary_sensor.garden_motion").state == "unknown"
    assert hass.states.get("binary_sensor.entrance_person_in_view").state == STATE_OFF
    assert hass.states.get("event.entrance_guardian") is not None
    assert hass.states.get("sensor.vigiora_site_unacknowledged_alerts").state == "5"
    assert hass.states.get("sensor.vigiora_site_cameras_online").state == "1"
    assert hass.states.get("sensor.vigiora_site_shop_occupancy").state == "3"
    assert hass.states.get("sensor.vigiora_site_shop_visitors_today").state == "42"
    assert hass.states.get("sensor.vigiora_site_visitors_today").state == "42"
    assert not hass.states.async_entity_ids("switch")
    assert not hass.states.async_entity_ids("select")


async def test_write_entities(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """A key with cameras:write gets the switches and the room AI mode select."""
    add_api(aioclient_mock, scopes=WRITE_SCOPES)
    await _setup(hass, config_entry)
    assert hass.states.get("switch.entrance_recording").state == STATE_ON
    assert hass.states.get("switch.garden_recording").state == STATE_OFF
    assert hass.states.get("switch.entrance_ai_guardian").state == STATE_OFF
    assert hass.states.get("select.home_ai_mode").state == "both"


async def test_no_analytics(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Analytics sensors are skipped when the endpoints do not answer."""
    add_api(aioclient_mock, analytics=False)
    await _setup(hass, config_entry)
    assert hass.states.get("sensor.vigiora_site_shop_occupancy") is None
    assert hass.states.get("sensor.vigiora_site_visitors_today") is None


async def test_camera_image(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """The camera serves the snapshot endpoint's JPEG."""
    from homeassistant.components.camera import async_get_image

    add_api(aioclient_mock)
    await _setup(hass, config_entry)
    image = await async_get_image(hass, "camera.entrance")
    assert image.content == b"\xff\xd8jpeg"


async def test_auth_failure_starts_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """A revoked key puts the entry in reauth."""
    add_api(aioclient_mock, me_status=HTTPStatus.UNAUTHORIZED)
    config_entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert any(f["context"]["source"] == "reauth" for f in flows)


async def test_unload(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """The entry unloads."""
    add_api(aioclient_mock)
    await _setup(hass, config_entry)
    assert await hass.config_entries.async_unload(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.NOT_LOADED


async def test_diagnostics_redacts_key(
    hass: HomeAssistant,
    aioclient_mock: AiohttpClientMocker,
    config_entry: MockConfigEntry,
    hass_client,
) -> None:
    """Diagnostics never contain the key or camera addresses."""
    add_api(aioclient_mock)
    await _setup(hass, config_entry)
    diag = await get_diagnostics_for_config_entry(hass, hass_client, config_entry)
    text = str(diag)
    assert API_KEY not in text
    assert "rtsp://" not in text
    assert "aa:bb:cc:dd:ee:ff" not in text
    assert diag["entry"]["data"]["api_key"] == "**REDACTED**"
    assert diag["server_version"] == "1.6.13"


async def test_key_not_logged(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry, caplog
) -> None:
    """The key never reaches the log, even at debug level."""
    import logging

    caplog.set_level(logging.DEBUG)
    add_api(aioclient_mock)
    await _setup(hass, config_entry)
    assert API_KEY not in caplog.text
    assert API_KEY not in repr(config_entry.runtime_data.client)
