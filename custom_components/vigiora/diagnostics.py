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
    return {
        "entry": {
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": dict(entry.options),
        },
        "capabilities": async_redact_data(asdict(caps), TO_REDACT),
        "server_version": runtime.version,
        "event_stream": runtime.events.diagnostics(),
        "data": async_redact_data(asdict(data), TO_REDACT) if data else None,
        "motion": runtime.motion.data if runtime.motion else None,
    }
