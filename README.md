# Vigiora for Home Assistant

A [HACS](https://hacs.xyz) custom integration that brings a [Vigiora](https://vigiora.com) server into
Home Assistant: cameras, motion and presence, Guardian alerts and scene descriptions, analytics occupancy,
room AI mode, and actions (speak, PTZ, describe, detect objects, acknowledge alerts).

It uses only Vigiora's **public API v1** (`/api/v1`, OpenAPI 1.16.0) with a scoped API key. Live updates
come from the server's event stream (Server-Sent Events) with automatic reconnect and backoff; if the stream
is down, it falls back to polling every 60 seconds.

Status: 0.1.0, private preview.

## Install (HACS custom repository)

1. In Home Assistant open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/data-tamer/vigiora-homeassistant` with category **Integration**.
   (While the repository is private, HACS needs a GitHub token with access to it.)
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
| `camera` | `GET /cameras/{id}/snapshot` (snapshot only, see below) |
| `binary_sensor` Motion | `GET /motion/latest` every 10 s (on when `has_motion` and the sample is < 90 s old) |
| `binary_sensor` Person in view | `person`/`face:*` detections (`alert.created`) and Guardian `person_present_seconds`; stays on 2 min |
| `binary_sensor` Online | camera `status` (`online`/`standby`), pushed by `camera.online` / `camera.offline` |
| `event` Guardian | `alert` (from `alert.created`) and `scene_description` (from `guardian.event`) with `description`, `severity`, `rule`, `alert_id`, `entry_id`, `snapshot_url`, … |
| `switch` Recording | `PATCH /cameras/{id}` `recording_enabled` |
| `switch` AI Guardian | `POST /cameras/{id}/actions/enable-ai` / `disable-ai` (plan limit → clear error) |

Server: **Unacknowledged alerts**, **Cameras online**, **Visitors today**; per analytics area: **occupancy**
and **visitors today** (`/analytics/occupancy`, `/analytics/counts?period=today`).
Per room: **AI mode** select (`live` / `guardian` / `both`, `PATCH /rooms/{id}`).

`snapshot_url` attributes point at the Vigiora API and need the API key to be fetched.

**No live video stream:** API v1 does not expose an RTSP/HLS/WebRTC URL for third parties (a camera's
`stream_url` is its source address with credentials redacted), so cameras are snapshot-only.

Options (**Configure**): which cameras to expose, which event types, and whether every detection or only
detections promoted to alerts fire the `alert` event.

## Actions

| Action | Endpoint |
|---|---|
| `vigiora.speak` (`text`, and `camera`, `room_id` or `output_id`) | `POST /rooms/{id}/actions/speak` |
| `vigiora.ptz_goto_preset` (`camera`, `preset` name or token) | `GET /cameras/{id}/ptz/presets`, `POST …/presets/{token}/actions/goto` |
| `vigiora.ptz_move` (`camera`, `pan`, `tilt`, `zoom`, `duration_ms`) | `POST /cameras/{id}/ptz/actions/move` |
| `vigiora.describe_camera` (`camera`) → response | `POST /cameras/{id}/actions/analyze` |
| `vigiora.detect_objects` (`camera`, `prompts`) → response | `POST /cameras/{id}/actions/detect-objects` |
| `vigiora.acknowledge_alert` (`alert_id`) | `POST /alerts/{id}/actions/acknowledge` |

Vigiora speaks only through rooms: with `camera`, the camera's room is used; with only `output_id`, the
room that has that speaker.

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

Apache-2.0. Copyright 2026 Data Tamer.
