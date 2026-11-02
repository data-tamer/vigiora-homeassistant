"""The Vigiora integration (public API v1)."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from urllib.parse import urlsplit

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.typing import ConfigType

from .api import (
    VigioraAuthError,
    VigioraClient,
    VigioraError,
)
from .const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL, DOMAIN, default_title, server_name
from .coordinator import (
    Capabilities,
    VigioraCoordinator,
    VigioraEventStream,
    VigioraMotionCoordinator,
)
from .entity import server_device_info
from .services import async_setup_services
from .vigiora_things import (
    AiohttpTransport,
    ServerInfo,
    ThingsApiError,
    ThingsAuthError,
    ThingsClient,
    ThingsError,
)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
_LOGGER = logging.getLogger(__name__)


@dataclass
class VigioraRuntime:
    """Per-entry runtime objects."""

    client: VigioraClient
    coordinator: VigioraCoordinator
    motion: VigioraMotionCoordinator | None
    events: VigioraEventStream
    version: str | None
    things: ThingsClient
    server: ServerInfo | None = None
    # Device registry id of the server device: the parent (via_device_id) of the
    # camera and room devices.
    server_device_id: str | None = None


type VigioraConfigEntry = ConfigEntry[VigioraRuntime]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Register the integration's actions."""
    async_setup_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: VigioraConfigEntry) -> bool:
    """Set up Vigiora from a config entry."""
    session = async_get_clientsession(hass, verify_ssl=entry.data.get(CONF_VERIFY_SSL, True))
    client = VigioraClient(session, entry.data[CONF_URL], entry.data[CONF_API_KEY])

    try:
        me = await client.get_me()
    except VigioraAuthError as err:
        raise ConfigEntryAuthFailed(err.message) from err
    except VigioraError as err:
        raise ConfigEntryNotReady(f"Cannot reach the Vigiora server: {err}") from err

    auth = me.get("auth") or {}
    user = me.get("user") or {}
    caps = Capabilities(
        scopes=auth.get("scopes"),
        role=str(user.get("role") or "viewer"),
        username=str(user.get("username") or ""),
        key_id=auth.get("key_id"),
    )

    # Feature detection: GET /api/v1/server (API 1.17.0+; None = older).
    things = ThingsClient(AiohttpTransport(session), client.url, entry.data[CONF_API_KEY])
    try:
        caps.server = await things.server_info()
    except ThingsAuthError as err:
        raise ConfigEntryAuthFailed(str(err)) from err
    except ThingsApiError as err:
        _LOGGER.debug("Server identity not readable: %s", err.code)
    except ThingsError as err:
        raise ConfigEntryNotReady(f"Cannot reach the Vigiora server: {err}") from err

    coordinator = VigioraCoordinator(hass, entry, client, caps)
    coordinator.things_client = things
    if caps.things_mode:
        try:
            coordinator.catalog = await things.catalog()
        except ThingsError as err:
            _LOGGER.debug("Things catalogue not readable (%s): using API v1 entities", err)
            caps.server = None
        else:
            if not coordinator.catalog.compatible:
                _LOGGER.warning(
                    "Vigiora Things catalogue %s is not supported; using API v1 entities",
                    coordinator.catalog.version,
                )
                caps.server = None
    try:
        await coordinator.async_probe_capabilities()
    except VigioraAuthError as err:
        raise ConfigEntryAuthFailed(err.message) from err
    events = VigioraEventStream(hass, coordinator)
    coordinator.events = events
    await coordinator.async_config_entry_first_refresh()

    motion: VigioraMotionCoordinator | None = None
    if caps.motion_available:
        motion = VigioraMotionCoordinator(hass, entry, client)
        await motion.async_config_entry_first_refresh()

    version = (coordinator.data.status or {}).get("version") if coordinator.data else None
    if caps.server is not None and caps.server.version:
        version = caps.server.version
    entry.runtime_data = VigioraRuntime(
        client=client,
        coordinator=coordinator,
        motion=motion,
        events=events,
        version=version,
        things=things,
        server=caps.server,
    )

    _async_update_default_title(hass, entry, caps.server)

    # The server device first: camera and room devices hang off it.
    server_device = dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **server_device_info(entry)
    )
    entry.runtime_data.server_device_id = server_device.id
    _async_remove_stale_devices(hass, entry, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    events.start()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


def _async_update_default_title(
    hass: HomeAssistant, entry: VigioraConfigEntry, server: ServerInfo | None
) -> None:
    """Title the entry with the server's own name (GET /api/v1/server).

    Only an entry still carrying a default title ("Vigiora <host>", or the site label
    0.1.0 derived from the URL) is renamed; a title the person chose is kept.
    """
    if server is None or not server.name:
        return
    url = entry.data[CONF_URL]
    host = urlsplit(url).hostname or url
    defaults = {default_title(host), default_title(server_name(url))}
    wanted = default_title(server.name)
    if entry.title in defaults and entry.title != wanted:
        hass.config_entries.async_update_entry(entry, title=wanted)


def _async_remove_stale_devices(
    hass: HomeAssistant, entry: VigioraConfigEntry, coordinator: VigioraCoordinator
) -> None:
    """Remove camera and room devices that are gone or no longer selected."""
    keep = {entry.entry_id}
    keep |= {
        f"{entry.entry_id}_camera_{cid}"
        for cid in coordinator.data.cameras
        if coordinator.camera_selected(cid)
    }
    keep |= {f"{entry.entry_id}_room_{rid}" for rid in coordinator.data.rooms}
    dev_reg = dr.async_get(hass)
    for device in dr.async_entries_for_config_entry(dev_reg, entry.entry_id):
        ids = {i[1] for i in device.identifiers if i[0] == DOMAIN}
        if ids and not ids & keep:
            dev_reg.async_update_device(device.id, remove_config_entry_id=entry.entry_id)


async def _async_options_updated(hass: HomeAssistant, entry: VigioraConfigEntry) -> None:
    """Reload when the options change."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: VigioraConfigEntry) -> bool:
    """Unload a config entry."""
    await entry.runtime_data.events.stop()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
