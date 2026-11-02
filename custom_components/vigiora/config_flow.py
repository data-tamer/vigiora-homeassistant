"""Config flow for Vigiora.

One step ("user") with exactly `url`, `api_key` and `verify_ssl`, so the flow
can also be completed through Home Assistant's REST config-flow API:
POST /api/config/config_entries/flow {"handler": "vigiora"}, then
POST /api/config/config_entries/flow/<flow_id> {"url", "api_key", "verify_ssl"}.

No zeroconf/SSDP discovery: Vigiora servers do not advertise themselves on the
LAN (no mDNS/SSDP announcement in the server).
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    VigioraApiError,
    VigioraAuthError,
    VigioraClient,
    VigioraConnectionError,
    VigioraError,
    VigioraSSLError,
    has_scope,
    normalize_url,
)
from .const import (
    CONF_ALERTS_ONLY,
    CONF_API_KEY,
    CONF_CAMERAS,
    CONF_EVENT_TYPES,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEFAULT_EVENT_TYPES,
    DOMAIN,
    EVENT_TYPES,
    default_title,
    server_name,
)
from .vigiora_things import AiohttpTransport, ThingsClient, ThingsError

_LOGGER = logging.getLogger(__name__)

USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_URL): TextSelector(TextSelectorConfig(type=TextSelectorType.URL)),
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
        vol.Optional(CONF_VERIFY_SSL, default=True): bool,
    }
)
REAUTH_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_API_KEY): TextSelector(
            TextSelectorConfig(type=TextSelectorType.PASSWORD)
        ),
    }
)


class InvalidUrl(Exception):
    """The URL is not http(s)://host[:port]."""


class MissingScope(Exception):
    """The key lacks cameras:read."""


async def validate_input(
    hass: HomeAssistant, url: str, api_key: str, verify_ssl: bool
) -> dict[str, Any]:
    """Check the key with cheap v1 calls; return name, version and identity."""
    session = async_get_clientsession(hass, verify_ssl=verify_ssl)
    client = VigioraClient(session, url, api_key)
    me = await client.get_me()
    auth = me.get("auth") or {}
    user = me.get("user") or {}
    scopes = auth.get("scopes")
    if not has_scope(scopes, "cameras:read"):
        raise MissingScope
    version = None
    name = server_name(url)
    # API 1.17.0+: the server's own name and version, readable by any key.
    try:
        server = await ThingsClient(AiohttpTransport(session), url, api_key).server_info()
    except ThingsError as err:
        _LOGGER.debug("Server identity not readable: %s", err)
        server = None
    if server is not None:
        name = server.name or name
        version = server.version or None
    if (
        version is None
        and has_scope(scopes, "settings:read")
        and user.get("role")
        in (
            "admin",
            "operator",
        )
    ):
        try:
            version = (await client.get_system_status()).get("version")
        except VigioraApiError as err:
            _LOGGER.debug("System status not readable: %s", err.code)
    return {
        "name": name,
        "version": version or "?",
        "username": user.get("username"),
        "key_id": auth.get("key_id"),
    }


class VigioraConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Vigiora."""

    VERSION = 1

    async def _try(
        self, url: str, api_key: str, verify_ssl: bool, errors: dict[str, str]
    ) -> dict[str, Any] | None:
        try:
            return await validate_input(self.hass, url, api_key, verify_ssl)
        except VigioraAuthError:
            errors["base"] = "invalid_auth"
        except MissingScope:
            errors["base"] = "missing_scope"
        except VigioraSSLError:
            errors["base"] = "ssl_error"
        except VigioraConnectionError:
            errors["base"] = "cannot_connect"
        except VigioraApiError as err:
            errors["base"] = "invalid_auth" if err.status == 403 else "cannot_connect"
        except VigioraError:
            errors["base"] = "unknown"
        except Exception:
            _LOGGER.exception("Unexpected error validating the Vigiora server")
            errors["base"] = "unknown"
        return None

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The only setup step: url, api_key, verify_ssl."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                url = normalize_url(user_input[CONF_URL])
            except ValueError:
                errors[CONF_URL] = "invalid_url"
            else:
                verify = user_input.get(CONF_VERIFY_SSL, True)
                api_key = user_input[CONF_API_KEY].strip()
                info = await self._try(url, api_key, verify, errors)
                if info is not None:
                    await self.async_set_unique_id(url)
                    self._abort_if_unique_id_configured(
                        updates={CONF_API_KEY: api_key, CONF_VERIFY_SSL: verify}
                    )
                    return self.async_create_entry(
                        title=default_title(info["name"]),
                        data={CONF_URL: url, CONF_API_KEY: api_key, CONF_VERIFY_SSL: verify},
                        description_placeholders={
                            "name": info["name"],
                            "version": info["version"],
                        },
                    )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(USER_SCHEMA, user_input or {}),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """The key stopped working (revoked, expired)."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new key."""
        errors: dict[str, str] = {}
        entry = self._get_reauth_entry()
        if user_input is not None:
            api_key = user_input[CONF_API_KEY].strip()
            info = await self._try(
                entry.data[CONF_URL], api_key, entry.data.get(CONF_VERIFY_SSL, True), errors
            )
            if info is not None:
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_API_KEY: api_key}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=REAUTH_SCHEMA,
            errors=errors,
            description_placeholders={"url": entry.data[CONF_URL]},
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> VigioraOptionsFlow:
        """Options: cameras and event types."""
        return VigioraOptionsFlow()


class VigioraOptionsFlow(OptionsFlow):
    """Choose the cameras and the event types to expose."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The options form."""
        if user_input is not None:
            return self.async_create_entry(data=user_input)
        cameras: dict[str, str] = {}
        runtime = getattr(self.config_entry, "runtime_data", None)
        if runtime is not None and runtime.coordinator.data is not None:
            cameras = {
                str(cid): str(cam.get("name") or cid)
                for cid, cam in runtime.coordinator.data.cameras.items()
            }
        opts = self.config_entry.options
        schema = vol.Schema(
            {
                vol.Optional(
                    CONF_CAMERAS,
                    default=[c for c in opts.get(CONF_CAMERAS, []) if c in cameras],
                ): cv.multi_select(cameras),
                vol.Optional(
                    CONF_EVENT_TYPES, default=opts.get(CONF_EVENT_TYPES, DEFAULT_EVENT_TYPES)
                ): cv.multi_select({t: t for t in EVENT_TYPES}),
                vol.Optional(CONF_ALERTS_ONLY, default=opts.get(CONF_ALERTS_ONLY, True)): bool,
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
