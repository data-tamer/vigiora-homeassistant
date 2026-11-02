"""Async client for the Vigiora public API v1.

Only documented /api/v1 endpoints are used. The API key is sent in the
Authorization header and never logged or put in a URL.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass
import json
import logging
from typing import Any
from urllib.parse import urlsplit

import aiohttp

_LOGGER = logging.getLogger(__name__)

DEFAULT_TIMEOUT = aiohttp.ClientTimeout(total=20)
ANALYZE_TIMEOUT = aiohttp.ClientTimeout(total=120)
DETECT_TIMEOUT = aiohttp.ClientTimeout(total=45)
# The stream sends a heartbeat every 15 s and ends after about a minute.
STREAM_TIMEOUT = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=45)

# Confirm-class operations used by this integration (x-vigiora-confirm in the
# OpenAPI document). The authoritative list is GET /api/v1/agent-policy.
OP_UPDATE_CAMERA = "updateCamera"
OP_ENABLE_AI = "enableCameraAI"
OP_DISABLE_AI = "disableCameraAI"
OP_MOVE_PTZ = "movePTZ"
OP_GOTO_PRESET = "gotoPTZPreset"
OP_SPEAK = "speakInRoom"
OP_UPDATE_ROOM = "updateRoom"


class VigioraError(Exception):
    """Base error."""


class VigioraConnectionError(VigioraError):
    """The server could not be reached (network, DNS, TLS, timeout)."""


class VigioraSSLError(VigioraConnectionError):
    """TLS verification failed."""


class VigioraApiError(VigioraError):
    """The API answered with an error document."""

    def __init__(
        self, status: int, code: str, message: str, details: dict[str, Any] | None
    ) -> None:
        """Initialise from the error document."""
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message
        self.details = details or {}


class VigioraAuthError(VigioraApiError):
    """401: the key is missing, wrong, expired or revoked."""


class VigioraApprovalRequired(VigioraError):
    """A confirm-class operation while human in the loop is on.

    Vigiora asks AI agents and automations to get a person's approval for
    these operations. This integration never approves on the user's behalf.
    """

    def __init__(self, operation_id: str) -> None:
        """Initialise with the operation that needs approval."""
        super().__init__(f"{operation_id} needs human approval")
        self.operation_id = operation_id


@dataclass(slots=True)
class SSEFrame:
    """One frame of the event stream."""

    event: str | None
    id: str | None
    data: str | None
    comment: bool = False


def normalize_url(url: str) -> str:
    """Return scheme://host[:port] for a user-entered server URL.

    Raises ValueError for anything that is not an http(s) URL with a host.
    """
    url = url.strip()
    if "://" not in url:
        url = f"https://{url}"
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("invalid url")
    if parts.username or parts.password:
        raise ValueError("credentials in url")
    netloc = parts.hostname
    if ":" in netloc:  # IPv6 literal
        netloc = f"[{netloc}]"
    if parts.port:
        netloc = f"{netloc}:{parts.port}"
    return f"{parts.scheme}://{netloc}"


def has_scope(scopes: list[str] | None, scope: str) -> bool:
    """Whether the effective scopes allow `scope` (implications per the API)."""
    if scopes is None:  # user session: every scope
        return True
    if "admin" in scopes or scope in scopes:
        return True
    resource, _, action = scope.partition(":")
    if action == "read":
        if f"{resource}:write" in scopes:
            return True
        if resource == "cameras" and "cameras:control" in scopes:
            return True
    return False


class VigioraClient:
    """Thin async wrapper around /api/v1."""

    def __init__(self, session: aiohttp.ClientSession, url: str, api_key: str) -> None:
        """Initialise the client."""
        self._session = session
        self.url = url.rstrip("/")
        self._api_key = api_key

    def __repr__(self) -> str:
        """Never include the key."""
        return f"VigioraClient({self.url})"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}

    async def _raise_for(self, resp: aiohttp.ClientResponse) -> None:
        if resp.status < 400:
            return
        code, message, details = "http_error", getattr(resp, "reason", None) or "", {}
        try:
            body = await resp.json(content_type=None)
            err = body.get("error") if isinstance(body, dict) else None
            if isinstance(err, dict):
                code = str(err.get("code") or code)
                message = str(err.get("message") or message)
                details = err.get("details") if isinstance(err.get("details"), dict) else {}
        except (aiohttp.ContentTypeError, ValueError, aiohttp.ClientError):
            pass
        if resp.status == 401:
            raise VigioraAuthError(resp.status, code, message, details)
        if code == "confirmation_required":
            # REST never answers this today (MCP only); treat it the same way
            # should a future server version add it.
            raise VigioraApprovalRequired(str(details.get("operation") or "operation"))
        raise VigioraApiError(resp.status, code, message, details)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        timeout: aiohttp.ClientTimeout = DEFAULT_TIMEOUT,
        operation_id: str | None = None,
        raw: bool = False,
    ) -> Any:
        """Call the API; returns the decoded JSON (or bytes when raw)."""
        if operation_id is not None:
            await self.ensure_no_approval_needed(operation_id)
        try:
            async with self._session.request(
                method,
                f"{self.url}{path}",
                params=params,
                json=json_body,
                headers=self._headers,
                timeout=timeout,
            ) as resp:
                await self._raise_for(resp)
                if raw:
                    return await resp.read()
                if resp.status == 204:
                    return None
                return await resp.json(content_type=None)
        except aiohttp.ClientConnectorCertificateError as err:
            raise VigioraSSLError(str(err)) from err
        except aiohttp.ClientSSLError as err:
            raise VigioraSSLError(str(err)) from err
        except (aiohttp.ClientError, TimeoutError) as err:
            raise VigioraConnectionError(type(err).__name__) from err

    async def ensure_no_approval_needed(self, operation_id: str) -> None:
        """Refuse a confirm-class operation while human in the loop is on.

        The policy is read fresh for every such call (fail closed: an error
        reading it aborts the operation).
        """
        policy = await self.get_agent_policy()
        if policy.get("human_in_the_loop") and operation_id in (
            policy.get("confirm_operations") or []
        ):
            raise VigioraApprovalRequired(operation_id)

    # --- reads -------------------------------------------------------------

    async def get_me(self) -> dict[str, Any]:
        """GET /api/v1/me (any credential)."""
        return await self.request("GET", "/api/v1/me")

    async def get_agent_policy(self) -> dict[str, Any]:
        """GET /api/v1/agent-policy (any credential)."""
        return await self.request("GET", "/api/v1/agent-policy")

    async def get_system_status(self) -> dict[str, Any]:
        """GET /api/v1/system/status (settings:read, admin/operator)."""
        return await self.request("GET", "/api/v1/system/status")

    async def _paged(
        self, path: str, params: dict[str, Any] | None = None, max_pages: int = 20
    ) -> list[dict[str, Any]]:
        params = {"limit": 200, **(params or {})}
        out: list[dict[str, Any]] = []
        for _ in range(max_pages):
            page = await self.request("GET", path, params=params)
            out.extend(page.get("data") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                break
            params = {**params, "cursor": cursor}
        return out

    async def list_cameras(self) -> list[dict[str, Any]]:
        """GET /api/v1/cameras (cameras:read), every page."""
        return await self._paged("/api/v1/cameras")

    async def get_snapshot(self, camera_id: int, width: int | None = None) -> bytes:
        """GET /api/v1/cameras/{id}/snapshot (cameras:read) -> JPEG."""
        return await self.request("GET", f"/api/v1/cameras/{camera_id}/snapshot", raw=True)

    async def list_rooms(self) -> list[dict[str, Any]]:
        """GET /api/v1/rooms (settings:read)."""
        return await self._paged("/api/v1/rooms")

    async def list_room_outputs(self, room_id: int) -> list[dict[str, Any]]:
        """GET /api/v1/rooms/{id}/outputs (settings:read)."""
        page = await self.request("GET", f"/api/v1/rooms/{room_id}/outputs")
        return page.get("data") or []

    async def get_occupancy(self) -> list[dict[str, Any]]:
        """GET /api/v1/analytics/occupancy (analytics:read)."""
        body = await self.request("GET", "/api/v1/analytics/occupancy")
        return body.get("areas") or []

    async def get_area_counts_today(self, area_id: int) -> dict[str, Any]:
        """GET /api/v1/analytics/counts?area_id=&period=today (analytics:read)."""
        return await self.request(
            "GET", "/api/v1/analytics/counts", params={"area_id": area_id, "period": "today"}
        )

    async def get_motion_latest(self) -> dict[str, Any]:
        """GET /api/v1/motion/latest (alerts:read) -> {camera_id: sample}."""
        body = await self.request("GET", "/api/v1/motion/latest")
        motion = body.get("motion") if isinstance(body, dict) else None
        return motion if isinstance(motion, dict) else {}

    async def count_unacknowledged_alerts(self, max_pages: int = 5) -> tuple[int, bool]:
        """Count unacknowledged alerts by listing them (alerts:read).

        Used when system status is not readable by the key. Returns the count
        and whether it was capped.
        """
        params: dict[str, Any] = {"limit": 200, "acknowledged": "false", "is_alert": "true"}
        total = 0
        for _ in range(max_pages):
            page = await self.request("GET", "/api/v1/alerts", params=params)
            total += len(page.get("data") or [])
            cursor = page.get("next_cursor")
            if not cursor:
                return total, False
            params = {**params, "cursor": cursor}
        return total, True

    async def list_alerts(self, **params: Any) -> list[dict[str, Any]]:
        """GET /api/v1/alerts, one page (alerts:read)."""
        page = await self.request("GET", "/api/v1/alerts", params=params)
        return page.get("data") or []

    async def list_guardian_entries(self, **params: Any) -> list[dict[str, Any]]:
        """GET /api/v1/guardian/entries, one page (guardian:read)."""
        page = await self.request("GET", "/api/v1/guardian/entries", params=params)
        return page.get("data") or []

    async def get_guardian_entry(self, entry_id: int) -> dict[str, Any]:
        """GET /api/v1/guardian/entries/{id} (guardian:read)."""
        return await self.request("GET", f"/api/v1/guardian/entries/{entry_id}")

    async def list_events(self, **params: Any) -> dict[str, Any]:
        """GET /api/v1/events, one page (events:read)."""
        return await self.request("GET", "/api/v1/events", params=params)

    async def list_ptz_presets(self, camera_id: int) -> list[dict[str, Any]]:
        """GET /api/v1/cameras/{id}/ptz/presets (cameras:control)."""
        page = await self.request("GET", f"/api/v1/cameras/{camera_id}/ptz/presets")
        return page.get("data") or []

    # --- writes / actions --------------------------------------------------

    async def set_recording(self, camera_id: int, enabled: bool) -> dict[str, Any]:
        """PATCH /api/v1/cameras/{id} recording_enabled (cameras:write, confirm)."""
        return await self.request(
            "PATCH",
            f"/api/v1/cameras/{camera_id}",
            json_body={"recording_enabled": enabled},
            operation_id=OP_UPDATE_CAMERA,
        )

    async def set_ai(self, camera_id: int, enabled: bool) -> dict[str, Any]:
        """POST /api/v1/cameras/{id}/actions/enable-ai|disable-ai (cameras:write, confirm)."""
        verb = "enable-ai" if enabled else "disable-ai"
        return await self.request(
            "POST",
            f"/api/v1/cameras/{camera_id}/actions/{verb}",
            operation_id=OP_ENABLE_AI if enabled else OP_DISABLE_AI,
        )

    async def set_room_ai_mode(self, room_id: int, mode: str) -> dict[str, Any]:
        """PATCH /api/v1/rooms/{id} ai_mode (cameras:write, confirm)."""
        return await self.request(
            "PATCH",
            f"/api/v1/rooms/{room_id}",
            json_body={"ai_mode": mode},
            operation_id=OP_UPDATE_ROOM,
        )

    async def speak(
        self, room_id: int, text: str, output_id: int | None = None, volume: float | None = None
    ) -> dict[str, Any]:
        """POST /api/v1/rooms/{id}/actions/speak (cameras:control, confirm)."""
        body: dict[str, Any] = {"text": text}
        if output_id is not None:
            body["output_id"] = output_id
        if volume is not None:
            body["volume"] = volume
        return await self.request(
            "POST", f"/api/v1/rooms/{room_id}/actions/speak", json_body=body, operation_id=OP_SPEAK
        )

    async def ptz_goto_preset(self, camera_id: int, token: str) -> dict[str, Any]:
        """POST …/ptz/presets/{token}/actions/goto (cameras:control, confirm)."""
        return await self.request(
            "POST",
            f"/api/v1/cameras/{camera_id}/ptz/presets/{token}/actions/goto",
            operation_id=OP_GOTO_PRESET,
        )

    async def ptz_move(
        self, camera_id: int, pan: float, tilt: float, zoom: float, duration_ms: int
    ) -> dict[str, Any]:
        """POST /api/v1/cameras/{id}/ptz/actions/move (cameras:control, confirm)."""
        return await self.request(
            "POST",
            f"/api/v1/cameras/{camera_id}/ptz/actions/move",
            json_body={"pan": pan, "tilt": tilt, "zoom": zoom, "duration_ms": duration_ms},
            operation_id=OP_MOVE_PTZ,
        )

    async def analyze(
        self, camera_id: int, include_objects: bool = False, prompts: list[str] | None = None
    ) -> dict[str, Any]:
        """POST /api/v1/cameras/{id}/actions/analyze (guardian:write)."""
        body: dict[str, Any] = {}
        if include_objects:
            body["include_objects"] = True
            if prompts:
                body["prompts"] = prompts
        return await self.request(
            "POST",
            f"/api/v1/cameras/{camera_id}/actions/analyze",
            json_body=body,
            timeout=ANALYZE_TIMEOUT,
        )

    async def detect_objects(
        self, camera_id: int, prompts: list[str] | None, min_confidence: float | None = None
    ) -> dict[str, Any]:
        """POST /api/v1/cameras/{id}/actions/detect-objects (cameras:read)."""
        body: dict[str, Any] = {"annotate": False}
        if prompts:
            body["prompts"] = prompts
        if min_confidence is not None:
            body["min_confidence"] = min_confidence
        return await self.request(
            "POST",
            f"/api/v1/cameras/{camera_id}/actions/detect-objects",
            json_body=body,
            timeout=DETECT_TIMEOUT,
        )

    async def acknowledge_alert(self, alert_id: int) -> dict[str, Any]:
        """POST /api/v1/alerts/{id}/actions/acknowledge (alerts:write)."""
        return await self.request("POST", f"/api/v1/alerts/{alert_id}/actions/acknowledge")

    # --- event stream ------------------------------------------------------

    async def stream_events(
        self, *, types: list[str] | None = None, last_event_id: str | None = None
    ) -> AsyncIterator[SSEFrame]:
        """GET /api/v1/events/stream (events:read): yield SSE frames.

        Comments (heartbeats) are yielded with comment=True so the caller can
        track liveness. The generator ends when the server closes the stream.
        """
        headers = {**self._headers, "Accept": "text/event-stream", "Cache-Control": "no-cache"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        params = {"types": ",".join(types)} if types else None
        try:
            async with self._session.get(
                f"{self.url}/api/v1/events/stream",
                params=params,
                headers=headers,
                timeout=STREAM_TIMEOUT,
            ) as resp:
                await self._raise_for(resp)
                async for frame in parse_sse(resp.content):
                    yield frame
        except (aiohttp.ClientConnectorCertificateError, aiohttp.ClientSSLError) as err:
            raise VigioraSSLError(str(err)) from err
        except (aiohttp.ClientError, TimeoutError, asyncio.IncompleteReadError) as err:
            raise VigioraConnectionError(type(err).__name__) from err


async def parse_sse(content: aiohttp.StreamReader | Any) -> AsyncIterator[SSEFrame]:
    """Parse a text/event-stream body into frames (WHATWG rules, simplified)."""
    event: str | None = None
    ev_id: str | None = None
    data: list[str] = []
    async for raw in content:
        line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        if line == "":
            if data or event:
                yield SSEFrame(event=event, id=ev_id, data="\n".join(data) if data else None)
            event, data = None, []
            continue
        if line.startswith(":"):
            yield SSEFrame(event=None, id=None, data=None, comment=True)
            continue
        field, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if field == "event":
            event = value
        elif field == "data":
            data.append(value)
        elif field == "id":
            ev_id = value
    if data:
        yield SSEFrame(event=event, id=ev_id, data="\n".join(data))


def decode_event(frame: SSEFrame) -> dict[str, Any] | None:
    """Decode a frame's JSON event, or None."""
    if not frame.data:
        return None
    try:
        body = json.loads(frame.data)
    except ValueError:
        _LOGGER.debug("Ignoring a non-JSON event frame")
        return None
    return body if isinstance(body, dict) else None
