"""Diagnostics for Vigiora (the API key and camera addresses are redacted)."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import VigioraConfigEntry
from .const import CONF_API_KEY

TO_REDACT = {
    CONF_API_KEY,
    "stream_url",
    "onvif_url",
    "hardware_id",
    "lat",
    "lon",
    "client_id",
    "username",
    "ai_guardian_prompt",
    "stt_prompt",
    "instruction",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: VigioraConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    runtime = entry.runtime_data
    coordinator = runtime.coordinator
    data = coordinator.data
    caps = coordinator.caps
    server = asdict(runtime.server) if runtime.server else None
    if server is not None:
        server["features"] = sorted(server["features"])
    caps_d = asdict(caps)
    caps_d["server"] = server
    data_d = asdict(data) if data else None
    if data_d is not None:
        data_d.pop("things", None)
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "capabilities": async_redact_data(caps_d, TO_REDACT),
        "server_version": runtime.version,
        "server": server,
        "things_mode": caps.things_mode,
        "things": sorted(data.things) if data else [],
        "event_stream": runtime.events.diagnostics(),
        "data": async_redact_data(data_d, TO_REDACT) if data_d else None,
        "motion": runtime.motion.data if runtime.motion else None,
    }
