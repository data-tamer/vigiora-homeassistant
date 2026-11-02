# Vigiora for Home Assistant

A [HACS](https://hacs.xyz) custom integration that brings a [Vigiora](https://vigiora.com) server into
Home Assistant: cameras, motion and presence, Guardian alerts and scene descriptions, analytics occupancy,
room AI mode, and actions (speak, PTZ, describe, detect objects, acknowledge alerts).

It uses only Vigiora's **public API v1** (`/api/v1`) with a scoped API key. On a server with API 1.18.0 or
later it uses the platform-neutral **Things model** (`/api/v1/things`, events in the Things envelope) through
the `vigiora_things` client library in this repository; older servers (API 1.16) keep working with the 0.1.0
behaviour (feature detection via `GET /api/v1/server`). Live updates come from the server's event stream
(Server-Sent Events) with automatic reconnect and backoff; if the stream is down, it falls back to polling
every 60 seconds.

Status: 0.2.1, beta.

## Install (HACS custom repository)

1. In Home Assistant open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/data-tamer/vigiora-homeassistant` with category **Integration**.
3. Install **Vigiora**, then restart Home Assistant.
4. **Settings → Devices & services → Add integration → Vigiora.** Enter:
   - **Server URL**: `https://<site>.vigiora.com`, or `http(s)://host:port` on your LAN;
   - **API key**: a `vgk_…` key from Vigiora (**Settings → API keys**);
   - **Verify the TLS certificate**: on by default; turn it off only for a self-signed LAN certificate.

The integration checks the key with `GET /api/v1/me` and shows the server name and version.

Manual install: copy `custom_components/vigiora` into your Home Assistant `config/custom_components/`.

### Programmatic setup (REST config flow)

The config flow has a single step, so it can be completed via Home Assistant's REST API (used by the
Vigiora one-click installer):

```http
POST /api/config/config_entries/flow
{"handler": "vigiora"}

POST /api/config/config_entries/flow/<flow_id>
{"url": "https://site.vigiora.com", "api_key": "vgk_…", "verify_ssl": true}
```

`verify_ssl` is optional (default `true`).

## API key scopes

| What | Scopes needed |
|---|---|
| Cameras (snapshots), online sensors, detect objects | `cameras:read` (**required**) |
| Motion, person in view, alert events, unacknowledged alerts | `alerts:read` |
| Scene descriptions (Guardian), person presence | `guardian:read` |
| Live push (otherwise polled every 60 s) | `events:read` |
| Rooms, system status (version, alert count) | `settings:read` (status: admin or operator users) |
| Occupancy and visitors per analytics area | `analytics:read` (and the Analytics add-on) |
| Recording / AI Guardian switches, room AI mode select | `cameras:write` (admin or operator users) |
| Speak, PTZ preset and move | `cameras:control` |
| Describe camera (Guardian analysis, uses AI credits) | `guardian:write` |
| Acknowledge alert | `alerts:write` |

**Read-only** (cameras, sensors and events): `cameras:read alerts:read guardian:read events:read
settings:read analytics:read`. Entities and actions the key cannot use are not created.

## Entities

Per camera (device per camera, under the server device):

| Entity | Source |
|---|---|
| `camera` | snapshot `GET /cameras/{id}/snapshot`; **live video** (API 1.17+): a signed HLS link from `POST /cameras/{id}/actions/stream-url`, renewed before it expires |
| `binary_sensor` Motion | API 1.18+: pushed `motion.started` / `motion.ended` (no polling); older: `GET /motion/latest` every 10 s |
| `binary_sensor` Person in view | API 1.18+: pushed `presence.changed` (detector presence); otherwise `person`/`face:*` detections and Guardian presence, on for 2 min |
| `binary_sensor` Online | camera `status`, pushed by `camera.online` / `camera.offline` |
| `event` Guardian | `alert` (from `alert.created`) and `scene_description` (from `guardian.event`, with `person_present`, `detections`, `detector_checked` on API 1.17+ — no extra request) with `description`, `severity`, `rule`, `alert_id`, `entry_id`, `snapshot_url`, … |
| `switch` Recording | Things command `recording.turn_on/off` (older: `PATCH /cameras/{id}`) |
| `switch` AI Guardian | Things command `ai_guardian.turn_on/off` (older: `enable-ai` / `disable-ai`) |

Server: **Unacknowledged alerts** (API 1.17+: `GET /alerts/summary`, any key with `alerts:read`), **Cameras
online**, **Visitors today**; per analytics area: **occupancy** and **visitors today**. Per room: **AI mode**
select. The server device shows the server's own name, version and id (`GET /api/v1/server`).

`snapshot_url` attributes: on API 1.17+ a signed link valid for 15 minutes that needs no API key (usable in
notifications); on older servers they point at the API and need the key.

On a Things server the entities are built from one table (`custom_components/vigiora/things_map.py`:
capability → platform, device class, state field, commands). `contrib/mqtt_bridge/vigiora_mqtt.py` is a second
adapter over the same library with its own table (MQTT with Home Assistant MQTT discovery), as a proof that
the layer is reusable.

Options (**Configure**): which cameras to expose, which event types, and whether every detection or only
detections promoted to alerts fire the `alert` event.

## Actions

| Action | Endpoint |
|---|---|
| `vigiora.speak` (`text`, and `camera`/`camera_id`, `room_id` or `output_id`) | API 1.17+ with a camera: `POST /cameras/{id}/actions/speak`; else `POST /rooms/{id}/actions/speak` |
| `vigiora.ptz_goto_preset` (`camera`, `preset` name or token) | `GET /cameras/{id}/ptz/presets`, `POST …/presets/{token}/actions/goto` |
| `vigiora.ptz_move` (`camera`, `pan`, `tilt`, `zoom`, `duration_ms`) | `POST /cameras/{id}/ptz/actions/move` |
| `vigiora.describe_camera` (`camera`) → response | `POST /cameras/{id}/actions/analyze` |
| `vigiora.detect_objects` (`camera`, `prompts`) → response | `POST /cameras/{id}/actions/detect-objects` |
| `vigiora.acknowledge_alert` (`alert_id`) | `POST /alerts/{id}/actions/acknowledge` |

With a camera on API 1.17+, the camera's own speaker is used (no room needed); a camera without a speaker
output, or an older server, speaks in the camera's room; with only `output_id`, the room that has that
speaker.

### Human in the loop

Vigiora can require a person's approval before AI agents and automations run risky operations
(*human in the loop*, on by default; see `GET /api/v1/agent-policy`). Before every such operation
(switches, AI mode, speak, PTZ) the integration reads the key's policy; when approval is required it
raises a clear error and **never approves on your behalf**. To let Home Assistant run them, an admin turns
human in the loop off for the key (or the server) in Vigiora.

Errors are reported clearly: `402` (plan limit, credits), `403` (missing scope, permission), `409`
(e.g. AI not enabled on the camera, PTZ not supported), `429` (rate limits).

## Security

- The API key is stored in the config entry, sent only in the `Authorization` header, never logged, and
  redacted in diagnostics (with camera URLs, MAC addresses and coordinates).
- TLS certificates are verified by default.
- No LAN discovery (zeroconf/SSDP): Vigiora servers do not advertise themselves on the network.

## Translations

English, plus Thai, Spanish, Lao, Portuguese, Japanese, German and French. **The non-English translations
are machine-translated** and await review by native speakers.

## Development

```bash
python3.13 -m venv .venv && .venv/bin/pip install -r requirements_test.txt
.venv/bin/ruff check . && .venv/bin/ruff format --check .
.venv/bin/python scripts/validate.py   # offline subset of the hassfest/HACS structure checks
.venv/bin/pytest -q
```

CI (`.github/workflows/validate.yml`) runs hassfest, HACS validation and the tests.

## Licence

MIT. Copyright 2026 Data Tamer.
