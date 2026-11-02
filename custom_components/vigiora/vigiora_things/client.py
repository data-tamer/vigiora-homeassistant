"""Async client for the Vigiora Things model (API v1).

The transport is pluggable (`Transport`): `AiohttpTransport` wraps an
aiohttp ClientSession; tests and other runtimes pass their own. The API key
travels in the Authorization header only, never in a URL, and is never logged
or included in a repr.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
import contextlib
from dataclasses import dataclass
import json
import logging
import random
from typing import Any, Protocol
from urllib.parse import urljoin

from .errors import ThingsApiError, ThingsAuthError, ThingsConnectionError, ThingsError
from .models import Catalog, ServerInfo, Thing, ThingEvent
from .sse import SSEFrame, parse_sse

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class Response:
    """A buffered response."""

    status: int
    body: bytes


class Transport(Protocol):
    """How the client talks HTTP."""

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Response:
        """One request, body buffered."""

    def stream(
        self, url: str, *, headers: dict[str, str], params: dict[str, Any] | None = None
    ) -> contextlib.AbstractAsyncContextManager[tuple[int, Any]]:
        """Open a streaming GET: (status, async iterable of lines or the error body)."""


class AiohttpTransport:
    """Transport over an aiohttp ClientSession."""

    def __init__(self, session: Any, timeout: float = 20.0) -> None:
        """Initialise with a session (owned by the caller)."""
        import aiohttp  # noqa: PLC0415 - optional dependency of the library

        self._aiohttp = aiohttp
        self._session = session
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._stream_timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=45)

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Response:
        """One request."""
        try:
            async with self._session.request(
                method, url, params=params, json=json_body, headers=headers, timeout=self._timeout
            ) as resp:
                return Response(resp.status, await resp.read())
        except (self._aiohttp.ClientError, TimeoutError) as err:
            raise ThingsConnectionError(type(err).__name__) from err

    @contextlib.asynccontextmanager
    async def stream(
        self, url: str, *, headers: dict[str, str], params: dict[str, Any] | None = None
    ) -> AsyncIterator[tuple[int, Any]]:
        """A streaming GET."""
        try:
            async with self._session.get(
                url, params=params, headers=headers, timeout=self._stream_timeout
            ) as resp:
                if resp.status >= 400:
                    yield resp.status, await resp.read()
                else:
                    yield resp.status, resp.content
        except (self._aiohttp.ClientError, TimeoutError, asyncio.IncompleteReadError) as err:
            raise ThingsConnectionError(type(err).__name__) from err


def _error(status: int, body: bytes) -> ThingsApiError:
    code, message, details = "http_error", "", {}
    with contextlib.suppress(ValueError, UnicodeDecodeError):
        doc = json.loads(body or b"{}")
        err = doc.get("error") if isinstance(doc, dict) else None
        if isinstance(err, dict):
            code = str(err.get("code") or code)
            message = str(err.get("message") or "")
            details = err.get("details") if isinstance(err.get("details"), dict) else {}
    cls = ThingsAuthError if status == 401 else ThingsApiError
    return cls(status, code, message, details)


@dataclass(slots=True)
class StreamControl:
    """A non-event frame of the stream: overflow, reset or shutdown."""

    kind: str
    data: dict[str, Any]


class ThingsClient:
    """The Things API: server identity, catalogue, things, commands, events."""

    def __init__(self, transport: Transport, base_url: str, api_key: str) -> None:
        """Initialise."""
        self._t = transport
        self.base_url = base_url.rstrip("/")
        self._api_key = api_key

    def __repr__(self) -> str:
        """Never include the key."""
        return f"ThingsClient({self.base_url})"

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}", "Accept": "application/json"}

    def absolute(self, url: str | None) -> str | None:
        """Resolve a server-relative link (signed media) against the base URL."""
        if not url:
            return None
        return urljoin(self.base_url + "/", url)

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        """Call the API and decode the JSON answer (ThingsApiError on 4xx/5xx)."""
        resp = await self._t.request(
            method,
            f"{self.base_url}{path}",
            headers=self._headers,
            params=params,
            json_body=json_body,
        )
        if resp.status >= 400:
            raise _error(resp.status, resp.body)
        if resp.status == 204 or not resp.body:
            return None
        try:
            return json.loads(resp.body)
        except ValueError as err:
            raise ThingsError("the server answered with something other than JSON") from err

    # --- feature detection ---------------------------------------------------

    async def server_info(self) -> ServerInfo | None:
        """GET /api/v1/server; None for a server older than API 1.17.0."""
        try:
            return ServerInfo.from_dict(await self.request("GET", "/api/v1/server"))
        except ThingsApiError as err:
            if err.status in (404, 405) and not isinstance(err, ThingsAuthError):
                return None
            raise

    # --- things --------------------------------------------------------------

    async def catalog(self) -> Catalog:
        """GET /api/v1/thing-capabilities."""
        return Catalog.from_dict(await self.request("GET", "/api/v1/thing-capabilities"))

    async def things(self, kind: str | None = None) -> list[Thing]:
        """GET /api/v1/things."""
        body = await self.request("GET", "/api/v1/things", params={"kind": kind} if kind else None)
        return [Thing.from_dict(t) for t in (body or {}).get("data") or [] if isinstance(t, dict)]

    async def thing(self, thing_id: str) -> Thing:
        """GET /api/v1/things/{id}."""
        return Thing.from_dict(await self.request("GET", f"/api/v1/things/{thing_id}"))

    async def command(
        self, thing_id: str, command: str, params: dict[str, Any] | None = None
    ) -> Any:
        """POST /api/v1/things/{id}/commands/{capability}.{command}; the operation's result."""
        body = await self.request(
            "POST", f"/api/v1/things/{thing_id}/commands/{command}", json_body=params or {}
        )
        return (body or {}).get("result")

    # --- events --------------------------------------------------------------

    async def events(
        self,
        *,
        types: list[str] | None = None,
        last_event_id: str | None = None,
        things_format: bool = True,
    ) -> AsyncIterator[ThingEvent | dict[str, Any] | StreamControl | None]:
        """GET /api/v1/events/stream: yield events until the server closes it.

        Yields ThingEvent (things format) or the v1 event dict, StreamControl
        for overflow/reset/shutdown, and None for a heartbeat.
        """
        headers = {**self._headers, "Accept": "text/event-stream", "Cache-Control": "no-cache"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        params: dict[str, Any] = {}
        if types:
            params["types"] = ",".join(types)
        if things_format:
            params["format"] = "things"
        async with self._t.stream(
            f"{self.base_url}/api/v1/events/stream", headers=headers, params=params or None
        ) as (status, body):
            if status >= 400:
                raise _error(status, body)
            async for frame in parse_sse(body):
                item = _frame_item(frame, things_format)
                if item is not _SKIP:
                    yield item


_SKIP = object()


def _frame_item(frame: SSEFrame, things_format: bool) -> Any:
    if frame.comment:
        return None
    if frame.event in ("overflow", "reset", "shutdown"):
        data: dict[str, Any] = {}
        with contextlib.suppress(ValueError, TypeError):
            data = json.loads(frame.data or "{}")
        return StreamControl(frame.event, data if isinstance(data, dict) else {})
    if not frame.data:
        return _SKIP
    try:
        body = json.loads(frame.data)
    except ValueError:
        return _SKIP
    if not isinstance(body, dict):
        return _SKIP
    return ThingEvent.from_dict(body) if things_format else body


class EventSubscription:
    """Consumes the event stream forever: reconnects with Last-Event-ID.

    `on_event` gets every event (ThingEvent or v1 dict) once, in order;
    `on_connected(bool)` the connection state. Backoff doubles from 1 s to
    5 minutes on errors (a 429's retry_after_seconds wins) and resets once a
    frame arrives. A 401 or 403 stops the subscription (raised to the caller
    of run()), since retrying cannot fix it.
    """

    def __init__(
        self,
        client: ThingsClient,
        on_event: Callable[[Any], Awaitable[None] | None],
        *,
        types: list[str] | None = None,
        things_format: bool = True,
        on_connected: Callable[[bool], None] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        backoff_initial: float = 1.0,
        backoff_max: float = 300.0,
    ) -> None:
        """Initialise."""
        self.client = client
        self.on_event = on_event
        self.types = types
        self.things_format = things_format
        self.on_connected = on_connected
        self._sleep = sleep
        self.backoff_initial = backoff_initial
        self.backoff_max = backoff_max
        self.last_event_id: str | None = None
        self.reconnects = 0
        self.last_error: str | None = None
        self._stopped = False
        self._seen: set[str] = set()

    def stop(self) -> None:
        """Stop after the current frame."""
        self._stopped = True

    def _connected(self, on: bool) -> None:
        if self.on_connected is not None:
            self.on_connected(on)

    async def _deliver(self, item: Any) -> None:
        ev_id = item.id if isinstance(item, ThingEvent) else item.get("id")
        if isinstance(ev_id, str) and ev_id:
            if ev_id in self._seen:
                return
            self._seen.add(ev_id)
            if len(self._seen) > 2000:
                self._seen = set(sorted(self._seen)[-1000:])
            if self.last_event_id is None or ev_id > self.last_event_id:
                self.last_event_id = ev_id
        res = self.on_event(item)
        if asyncio.iscoroutine(res):
            await res

    async def run(self, max_connections: int | None = None) -> None:
        """Run until stop() (or max_connections streams, for tests)."""
        backoff = self.backoff_initial
        connections = 0
        while not self._stopped:
            if max_connections is not None and connections >= max_connections:
                return
            connections += 1
            try:
                async for item in self.client.events(
                    types=self.types,
                    last_event_id=self.last_event_id,
                    things_format=self.things_format,
                ):
                    self._connected(True)
                    backoff = self.backoff_initial
                    if item is None:
                        continue
                    if isinstance(item, StreamControl):
                        if item.kind == "reset":
                            self.last_event_id = None
                        if item.kind == "shutdown":
                            break
                        continue
                    await self._deliver(item)
                    if self._stopped:
                        return
                self._connected(False)
                await self._sleep(1.0)  # the server's retry hint
            except asyncio.CancelledError:
                raise
            except ThingsApiError as err:
                self._connected(False)
                self.last_error = err.code
                if err.status in (401, 403):
                    raise
                retry = err.details.get("retry_after_seconds")
                delay = float(retry) if isinstance(retry, (int, float)) else backoff
                self.reconnects += 1
                await self._sleep(delay)
                backoff = min(backoff * 2, self.backoff_max)
            except ThingsError as err:
                self._connected(False)
                self.last_error = type(err).__name__
                self.reconnects += 1
                await self._sleep(backoff * (1 + random.random() / 10))  # noqa: S311
                backoff = min(backoff * 2, self.backoff_max)
