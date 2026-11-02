"""Vigiora actions (services)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import (
    HomeAssistant,
    ServiceCall,
    ServiceResponse,
    SupportsResponse,
    callback,
)
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .api import VigioraApiError, VigioraError
from .const import (
    ATTR_ALERT_ID,
    ATTR_CAMERA,
    ATTR_CAMERA_ID,
    ATTR_CONFIG_ENTRY_ID,
    ATTR_DURATION_MS,
    ATTR_INCLUDE_OBJECTS,
    ATTR_MIN_CONFIDENCE,
    ATTR_OUTPUT_ID,
    ATTR_PAN,
    ATTR_PRESET,
    ATTR_PROMPTS,
    ATTR_ROOM_ID,
    ATTR_TEXT,
    ATTR_TILT,
    ATTR_VOLUME,
    ATTR_ZOOM,
    DOMAIN,
    SERVICE_ACKNOWLEDGE_ALERT,
    SERVICE_DESCRIBE_CAMERA,
    SERVICE_DETECT_OBJECTS,
    SERVICE_PTZ_GOTO_PRESET,
    SERVICE_PTZ_MOVE,
    SERVICE_SPEAK,
)
from .entity import parse_camera_unique_id
from .errors import to_ha_error
from .vigiora_things.models import FEATURE_CAMERA_SPEAK

if TYPE_CHECKING:
    from . import VigioraConfigEntry

_PROMPTS = vol.All(
    cv.ensure_list, [vol.All(cv.string, vol.Length(min=1, max=40))], vol.Length(max=20)
)

_TARGET = {
    vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
    vol.Optional(ATTR_CAMERA): cv.entity_id,
    vol.Optional(ATTR_CAMERA_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
}

SPEAK_SCHEMA = vol.Schema(
    {
        **_TARGET,
        vol.Required(ATTR_TEXT): vol.All(cv.string, vol.Length(min=1, max=1000)),
        vol.Optional(ATTR_ROOM_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional(ATTR_OUTPUT_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
        vol.Optional(ATTR_VOLUME): vol.All(vol.Coerce(float), vol.Range(min=0, max=2)),
    }
)
GOTO_SCHEMA = vol.Schema({**_TARGET, vol.Required(ATTR_PRESET): cv.string})
MOVE_SCHEMA = vol.Schema(
    {
        **_TARGET,
        vol.Optional(ATTR_PAN, default=0): vol.All(vol.Coerce(float), vol.Range(min=-1, max=1)),
        vol.Optional(ATTR_TILT, default=0): vol.All(vol.Coerce(float), vol.Range(min=-1, max=1)),
        vol.Optional(ATTR_ZOOM, default=0): vol.All(vol.Coerce(float), vol.Range(min=-1, max=1)),
        vol.Optional(ATTR_DURATION_MS, default=400): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=5000)
        ),
    }
)
DESCRIBE_SCHEMA = vol.Schema(
    {
        **_TARGET,
        vol.Optional(ATTR_INCLUDE_OBJECTS, default=False): cv.boolean,
        vol.Optional(ATTR_PROMPTS): _PROMPTS,
    }
)
DETECT_SCHEMA = vol.Schema(
    {
        **_TARGET,
        vol.Optional(ATTR_PROMPTS): _PROMPTS,
        vol.Optional(ATTR_MIN_CONFIDENCE): vol.All(
            vol.Coerce(float), vol.Range(min=0.01, max=0.99)
        ),
    }
)
ACK_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_CONFIG_ENTRY_ID): cv.string,
        vol.Required(ATTR_ALERT_ID): vol.All(vol.Coerce(int), vol.Range(min=1)),
    }
)


def _loaded_entries(hass: HomeAssistant) -> list[VigioraConfigEntry]:
    return [
        e for e in hass.config_entries.async_entries(DOMAIN) if e.state is ConfigEntryState.LOADED
    ]


def _entry(hass: HomeAssistant, entry_id: str | None) -> VigioraConfigEntry:
    entries = _loaded_entries(hass)
    if entry_id:
        for e in entries:
            if e.entry_id == entry_id:
                return e
        raise ServiceValidationError(
            translation_domain=DOMAIN,
            translation_key="entry_not_found",
            translation_placeholders={"entry_id": entry_id},
        )
    if len(entries) == 1:
        return entries[0]
    raise ServiceValidationError(translation_domain=DOMAIN, translation_key="entry_required")


def _camera(
    hass: HomeAssistant, call: ServiceCall, required: bool = True
) -> tuple[VigioraConfigEntry, int | None]:
    """Resolve (entry, camera_id) from `camera` (an entity) or `camera_id`."""
    entity_id = call.data.get(ATTR_CAMERA)
    if entity_id:
        ent = er.async_get(hass).async_get(entity_id)
        parsed = parse_camera_unique_id(ent.unique_id) if ent and ent.platform == DOMAIN else None
        if parsed is None:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="not_a_camera",
                translation_placeholders={"entity_id": entity_id},
            )
        return _entry(hass, ent.config_entry_id), parsed[1]
    entry = _entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
    camera_id = call.data.get(ATTR_CAMERA_ID)
    if camera_id is None and required:
        raise ServiceValidationError(translation_domain=DOMAIN, translation_key="camera_required")
    return entry, camera_id


async def _speak(hass: HomeAssistant, call: ServiceCall) -> None:
    entry, camera_id = _camera(hass, call, required=False)
    runtime = entry.runtime_data
    client = runtime.client
    room_id: int | None = call.data.get(ATTR_ROOM_ID)
    output_id: int | None = call.data.get(ATTR_OUTPUT_ID)
    try:
        if (
            camera_id is not None
            and room_id is None
            and output_id is None
            and runtime.coordinator.caps.has(FEATURE_CAMERA_SPEAK)
        ):
            # API 1.17.0: the camera's own speaker, no room needed.
            volume = call.data.get(ATTR_VOLUME)
            try:
                await client.camera_speak(
                    camera_id,
                    call.data[ATTR_TEXT],
                    min(volume, 1.0) if volume is not None else None,
                )
            except VigioraApiError as err:
                if err.status != 404:
                    raise
                # No speaker output on the camera: speak in its room.
            else:
                return
        if room_id is None:
            rooms = runtime.coordinator.data.rooms if runtime.coordinator.data else {}
            if not rooms and runtime.coordinator.caps.can("settings:read"):
                rooms = {int(r["id"]): r for r in await client.list_rooms()}
            for rid, room in rooms.items():
                if camera_id is not None:
                    if any(c.get("camera_id") == camera_id for c in room.get("cameras") or []):
                        room_id = rid
                        break
                elif output_id is not None:
                    outputs = await client.list_room_outputs(rid)
                    if any(o.get("id") == output_id for o in outputs):
                        room_id = rid
                        break
        if room_id is None:
            # API v1 speaks only through rooms (POST /rooms/{id}/actions/speak).
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="no_room_for_speaker"
            )
        await client.speak(room_id, call.data[ATTR_TEXT], output_id, call.data.get(ATTR_VOLUME))
    except VigioraError as err:
        raise to_ha_error(err) from err


async def _ptz_goto(hass: HomeAssistant, call: ServiceCall) -> None:
    entry, camera_id = _camera(hass, call)
    client = entry.runtime_data.client
    preset = call.data[ATTR_PRESET]
    try:
        # Accept a preset token or its name.
        token = preset
        presets = await client.list_ptz_presets(camera_id)
        by_token = {p.get("token"): p for p in presets}
        if preset not in by_token:
            matches = [p for p in presets if str(p.get("name", "")).casefold() == preset.casefold()]
            if not matches:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="preset_not_found",
                    translation_placeholders={"preset": preset},
                )
            token = matches[0]["token"]
        await client.ptz_goto_preset(camera_id, token)
    except VigioraError as err:
        raise to_ha_error(err) from err


async def _ptz_move(hass: HomeAssistant, call: ServiceCall) -> None:
    entry, camera_id = _camera(hass, call)
    try:
        await entry.runtime_data.client.ptz_move(
            camera_id,
            call.data[ATTR_PAN],
            call.data[ATTR_TILT],
            call.data[ATTR_ZOOM],
            call.data[ATTR_DURATION_MS],
        )
    except VigioraError as err:
        raise to_ha_error(err) from err


async def _describe(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry, camera_id = _camera(hass, call)
    try:
        result = await entry.runtime_data.client.analyze(
            camera_id, call.data[ATTR_INCLUDE_OBJECTS], call.data.get(ATTR_PROMPTS)
        )
    except VigioraError as err:
        raise to_ha_error(err) from err
    return {
        "camera_id": result.get("camera_id", camera_id),
        "description": result.get("description", ""),
        "analyzed_at": result.get("analyzed_at"),
        "objects": result.get("objects") or [],
    }


async def _detect(hass: HomeAssistant, call: ServiceCall) -> ServiceResponse:
    entry, camera_id = _camera(hass, call)
    try:
        result = await entry.runtime_data.client.detect_objects(
            camera_id, call.data.get(ATTR_PROMPTS), call.data.get(ATTR_MIN_CONFIDENCE)
        )
    except VigioraError as err:
        raise to_ha_error(err) from err
    return {
        "camera_id": result.get("camera_id", camera_id),
        "objects": result.get("objects") or [],
        "frame_time": result.get("frame_time"),
        "model": result.get("model"),
    }


async def _acknowledge(hass: HomeAssistant, call: ServiceCall) -> None:
    entry = _entry(hass, call.data.get(ATTR_CONFIG_ENTRY_ID))
    try:
        await entry.runtime_data.client.acknowledge_alert(call.data[ATTR_ALERT_ID])
    except VigioraError as err:
        raise to_ha_error(err) from err
    await entry.runtime_data.coordinator.async_request_refresh()


@callback
def async_setup_services(hass: HomeAssistant) -> None:
    """Register the actions once for the domain."""

    def wrap(fn: Any) -> Any:
        async def handler(call: ServiceCall) -> Any:
            return await fn(hass, call)

        return handler

    hass.services.async_register(DOMAIN, SERVICE_SPEAK, wrap(_speak), schema=SPEAK_SCHEMA)
    hass.services.async_register(
        DOMAIN, SERVICE_PTZ_GOTO_PRESET, wrap(_ptz_goto), schema=GOTO_SCHEMA
    )
    hass.services.async_register(DOMAIN, SERVICE_PTZ_MOVE, wrap(_ptz_move), schema=MOVE_SCHEMA)
    hass.services.async_register(
        DOMAIN,
        SERVICE_DESCRIBE_CAMERA,
        wrap(_describe),
        schema=DESCRIBE_SCHEMA,
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_DETECT_OBJECTS,
        wrap(_detect),
        schema=DETECT_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN, SERVICE_ACKNOWLEDGE_ALERT, wrap(_acknowledge), schema=ACK_SCHEMA
    )
