"""Config flow tests."""

from __future__ import annotations

from http import HTTPStatus

from aiohttp import ClientConnectionError
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.test_util.aiohttp import AiohttpClientMocker

from custom_components.vigiora.config_flow import server_name
from custom_components.vigiora.const import (
    CONF_ALERTS_ONLY,
    CONF_API_KEY,
    CONF_CAMERAS,
    CONF_EVENT_TYPES,
    CONF_URL,
    CONF_VERIFY_SSL,
    DOMAIN,
)
from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.setup import async_setup_component

from .conftest import API_KEY, URL, add_api

USER_INPUT = {CONF_URL: f"{URL}/", CONF_API_KEY: API_KEY, CONF_VERIFY_SSL: True}


async def test_user_success(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A valid key creates the entry with name and version."""
    add_api(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Vigiora site"
    assert result["data"] == {CONF_URL: URL, CONF_API_KEY: API_KEY, CONF_VERIFY_SSL: True}
    assert result["description_placeholders"] == {"name": "site", "version": "1.6.13"}
    assert result["result"].unique_id == URL


async def test_user_bad_key(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """401 shows invalid_auth."""
    add_api(aioclient_mock, me_status=HTTPStatus.UNAUTHORIZED)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_auth"}


async def test_user_unreachable(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A connection error shows cannot_connect."""
    aioclient_mock.get(f"{URL}/api/v1/me", exc=ClientConnectionError())
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "cannot_connect"}


async def test_user_missing_scope(hass: HomeAssistant, aioclient_mock: AiohttpClientMocker) -> None:
    """A key without cameras:read is refused."""
    add_api(aioclient_mock, scopes=["alerts:read"])
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {"base": "missing_scope"}


async def test_user_invalid_url(hass: HomeAssistant) -> None:
    """A non-http URL is refused before any request."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_URL: "ftp://x"}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}


async def test_user_already_configured(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """The same server twice aborts."""
    config_entry.add_to_hass(hass)
    add_api(aioclient_mock)
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": config_entries.SOURCE_USER}
    )
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_rest_created_entry(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_client
) -> None:
    """The one-click installer path: HA's REST config-flow API, two POSTs, no other steps."""
    add_api(aioclient_mock)
    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    resp = await client.post("/api/config/config_entries/flow", json={"handler": "vigiora"})
    assert resp.status == HTTPStatus.OK
    body = await resp.json()
    assert body["type"] == "form"
    assert body["step_id"] == "user"
    resp = await client.post(
        f"/api/config/config_entries/flow/{body['flow_id']}",
        json={"url": URL, "api_key": API_KEY, "verify_ssl": True},
    )
    assert resp.status == HTTPStatus.OK
    body = await resp.json()
    assert body["type"] == "create_entry"
    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    assert entries[0].data[CONF_API_KEY] == API_KEY
    # The key never appears in the REST answer.
    assert API_KEY not in str(body)


async def test_rest_created_entry_verify_ssl_omitted(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, hass_client
) -> None:
    """verify_ssl is optional and defaults to true."""
    add_api(aioclient_mock)
    assert await async_setup_component(hass, "config", {})
    client = await hass_client()
    body = await (
        await client.post("/api/config/config_entries/flow", json={"handler": "vigiora"})
    ).json()
    body = await (
        await client.post(
            f"/api/config/config_entries/flow/{body['flow_id']}",
            json={"url": URL, "api_key": API_KEY},
        )
    ).json()
    assert body["type"] == "create_entry"
    assert hass.config_entries.async_entries(DOMAIN)[0].data[CONF_VERIFY_SSL] is True


async def test_reauth(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Reauth replaces the key."""
    config_entry.add_to_hass(hass)
    add_api(aioclient_mock)
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_API_KEY: "vgk_new"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_API_KEY] == "vgk_new"


async def test_options(
    hass: HomeAssistant, aioclient_mock: AiohttpClientMocker, config_entry: MockConfigEntry
) -> None:
    """Options choose cameras and event types."""
    add_api(aioclient_mock)
    config_entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    result = await hass.config_entries.options.async_init(config_entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {CONF_CAMERAS: ["1"], CONF_EVENT_TYPES: ["alert"], CONF_ALERTS_ONLY: True},
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert config_entry.options[CONF_CAMERAS] == ["1"]
    # Only camera 1 is exposed after the reload.
    assert hass.states.get("camera.entrance") is not None
    assert hass.states.get("camera.garden") is None


def test_server_name() -> None:
    """Names from URLs."""
    assert server_name("https://base01.vigiora.com") == "base01"
    assert server_name("https://lan.base01.vigiora.com:8444") == "base01"
    assert server_name("http://192.168.1.10:8080") == "192.168.1.10"
