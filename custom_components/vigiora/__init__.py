"""The Vigiora integration (public API v1)."""

from __future__ import annotations

from dataclasses import dataclass

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
from .const import CONF_API_KEY, CONF_URL, CONF_VERIFY_SSL, DOMAIN
from .coordinator import (
    Capabilities,
    VigioraCoordinator,
    VigioraEventStream,
    VigioraMotionCoordinator,
)
from .entity import server_device_info
from .services import async_setup_services

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CAMERA,
    Platform.EVENT,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
]

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


@dataclass
class VigioraRuntime:
    """Per-entry runtime objects."""

    client: VigioraClient
    coordinator: VigioraCoordinator
    motion: VigioraMotionCoordinator | None
    events: VigioraEventStream
    version: str | None


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

    coordinator = VigioraCoordinator(hass, entry, client, caps)
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
    entry.runtime_data = VigioraRuntime(
        client=client, coordinator=coordinator, motion=motion, events=events, version=version
    )

    # The server device first: camera and room devices hang off it.
    dr.async_get(hass).async_get_or_create(
        config_entry_id=entry.entry_id, **server_device_info(entry)
    )
    _async_remove_stale_devices(hass, entry, coordinator)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    events.start()
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))
    return True


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
