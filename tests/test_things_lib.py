"""vigiora_things: parsing, feature detection, the event subscription, mapping."""

from __future__ import annotations

from collections.abc import AsyncIterator
import contextlib
import json
from typing import Any

import pytest

from custom_components.vigiora.things_map import HA_TABLE, unique_id
from custom_components.vigiora.vigiora_things import (
    Catalog,
    EventSubscription,
    Response,
    ServerInfo,
    Thing,
    ThingEvent,
    ThingsApiError,
    ThingsAuthError,
    ThingsClient,
    ThingsConnectionError,
    apply_event,
    plan,
)

from .conftest import CATALOG, SERVER, THINGS


class FakeTransport:
    """Scripted responses; streams are lists of SSE byte lines or exceptions."""

    def __init__(self) -> None:
        self.responses: dict[tuple[str, str], Response] = {}
        self.streams: list[Any] = []
        self.requests: list[tuple[str, str, dict[str, str], Any, Any]] = []
        self.stream_headers: list[dict[str, str]] = []
        self.stream_params: list[Any] = []

    def add(self, method: str, path: str, status: int, body: Any) -> None:
        raw = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.responses[(method, path)] = Response(status, raw)

    async def request(self, method, url, *, headers, params=None, json_body=None) -> Response:
        path = url.split("site.test", 1)[1]
        self.requests.append((method, path, headers, params, json_body))
        return self.responses[(method, path)]

    @contextlib.asynccontextmanager
    async def stream(self, url, *, headers, params=None) -> AsyncIterator[tuple[int, Any]]:
        self.stream_headers.append(headers)
        self.stream_params.append(params)
        item = self.streams.pop(0)
        if isinstance(item, Exception):
            raise item
        status, lines = item

        async def gen() -> AsyncIterator[bytes]:
            for line in lines:
                yield line

        yield status, (gen() if status < 400 else json.dumps(lines).encode())


def envelope(ev_id: str, typ: str, subject: str = "camera:1", data: dict | None = None) -> bytes:
    body = {
        "specversion": "1.0",
        "id": ev_id,
        "type": typ,
        "subject": subject,
        "source": f"vigiora://s/things/{subject}",
        "time": "2099-01-01T00:00:00Z",
        "data": data or {},
        "vigioracapability": typ.split(".")[1],
        "vigioracatalogversion": "1.0",
        "vigioraeventtype": typ.removeprefix("vigiora."),
    }
    return f"id: {ev_id}\nevent: {typ}\ndata: {json.dumps(body)}\n".encode()


def frames(*events: bytes) -> list[bytes]:
    out: list[bytes] = [b"retry: 1000\n", b"\n", b": heartbeat\n", b"\n"]
    for e in events:
        out.extend(line + b"\n" for line in e.rstrip(b"\n").split(b"\n"))
        out.append(b"\n")
    return out


def client(t: FakeTransport) -> ThingsClient:
    return ThingsClient(t, "https://site.test", "vgk_secret_never_logged")


def test_parse_models() -> None:
    server = ServerInfo.from_dict(SERVER)
    assert server.has("things") and not server.has("teleport")
    things = [Thing.from_dict(t) for t in THINGS]
    cam = things[1]
    assert cam.id == "camera:1" and cam.object_id == 1 and cam.kind == "camera"
    assert cam.state("recording", "on") is True
    assert cam.offers("ptz.move") and not cam.offers("ptz.teleport")
    assert cam.state("nothing", "x", "d") == "d"
    # Unknown fields and capabilities are ignored, never fatal.
    odd = Thing.from_dict(
        {"id": "toaster:1", "kind": "toaster", "capabilities": [{"name": "toast", "x": 1}], "y": 2}
    )
    assert odd.has("toast") and odd.object_id == 1
    cat = Catalog.from_dict(CATALOG)
    assert cat.compatible
    assert cat.command("recording.turn_off").operations["camera"] == "updateCamera"
    assert cat.command("recording.turn_off").hitl == "confirm"
    assert cat.command("nothing.run") is None
    assert not Catalog.from_dict({**CATALOG, "version": "2.0"}).compatible
    ev = ThingEvent.from_dict(
        json.loads(envelope("01A", "vigiora.motion.started").split(b"data: ", 1)[1])
    )
    assert ev.name == "started" and ev.camera_id == 1
    assert ev.to_v1()["type"] == "motion.started" and ev.to_v1()["camera_id"] == 1


def test_apply_event_reducer() -> None:
    things = {t["id"]: Thing.from_dict(t) for t in THINGS}

    def ev(typ: str, data: dict | None = None, subject: str = "camera:1") -> ThingEvent:
        return ThingEvent(id="x", type=typ, subject=subject, time=None, data=data or {})

    assert apply_event(things, ev("vigiora.motion.started", {"started_at": "t"})) is not None
    assert things["camera:1"].state("motion", "active") is True
    assert (
        apply_event(things, ev("vigiora.motion.started", {"started_at": "t"})) is None
    )  # unchanged
    apply_event(things, ev("vigiora.presence.changed", {"label": "person", "present": True}))
    assert things["camera:1"].state("presence", "person") is True
    assert (
        apply_event(things, ev("vigiora.presence.changed", {"label": "dog", "present": True}))
        is None
    )
    apply_event(things, ev("vigiora.device.offline"))
    assert things["camera:1"].online is False
    assert apply_event(things, ev("vigiora.alert.created")) is None  # no state change
    assert apply_event(things, ev("vigiora.motion.started", subject="camera:99")) is None


async def test_feature_detection() -> None:
    t = FakeTransport()
    c = client(t)
    t.add(
        "GET", "/api/v1/server", 404, {"error": {"code": "not_found", "message": "", "details": {}}}
    )
    assert await c.server_info() is None  # an older server
    t.add("GET", "/api/v1/server", 200, SERVER)
    assert (await c.server_info()).name == "shop01"
    t.add(
        "GET",
        "/api/v1/server",
        401,
        {"error": {"code": "unauthenticated", "message": "", "details": {}}},
    )
    with pytest.raises(ThingsAuthError):
        await c.server_info()
    # The key is only ever in the Authorization header.
    _method, path, headers, params, _ = t.requests[0]
    assert headers["Authorization"] == "Bearer vgk_secret_never_logged"
    assert "vgk_" not in path and not params
    assert "vgk_" not in repr(c)
    assert c.absolute("/api/v1/media/a/image") == "https://site.test/api/v1/media/a/image"


async def test_things_and_commands() -> None:
    t = FakeTransport()
    c = client(t)
    t.add("GET", "/api/v1/things", 200, {"data": THINGS, "next_cursor": None})
    things = await c.things()
    assert [x.id for x in things][:2] == ["server", "camera:1"]
    t.add(
        "POST",
        "/api/v1/things/camera:1/commands/speak.say",
        200,
        {
            "thing_id": "camera:1",
            "command": "speak.say",
            "operation_id": "speakOnCamera",
            "result": {"ok": True},
        },
    )
    assert await c.command("camera:1", "speak.say", {"text": "hi"}) == {"ok": True}
    assert t.requests[-1][4] == {"text": "hi"}
    t.add(
        "POST",
        "/api/v1/things/camera:1/commands/ptz.move",
        400,
        {"error": {"code": "validation_error", "message": "pan", "details": {"field": "pan"}}},
    )
    with pytest.raises(ThingsApiError) as err:
        await c.command("camera:1", "ptz.move", {"pan": 5})
    assert err.value.code == "validation_error" and err.value.details["field"] == "pan"


async def test_subscription_reconnects_with_last_event_id() -> None:
    t = FakeTransport()
    t.streams = [
        ThingsConnectionError("ClientConnectorError"),  # first try fails
        (
            200,
            frames(
                envelope("01A", "vigiora.motion.started"), envelope("01B", "vigiora.motion.ended")
            ),
        ),
        (
            200,
            frames(
                envelope("01B", "vigiora.motion.ended"),
                envelope("01C", "vigiora.presence.changed", data={"present": True}),
            ),
        ),
        (
            429,
            {
                "error": {
                    "code": "stream_limit",
                    "message": "",
                    "details": {"retry_after_seconds": 5},
                }
            },
        ),
        (200, [b"event: reset\n", b'data: {"reason":"last_event_id_expired"}\n', b"\n"]),
        (200, [b"event: shutdown\n", b"data: {}\n", b"\n"]),
    ]
    got: list[str] = []
    sleeps: list[float] = []
    states: list[bool] = []

    async def sleep(s: float) -> None:
        sleeps.append(s)

    sub = EventSubscription(
        client(t),
        lambda e: got.append(e.id),
        sleep=sleep,
        on_connected=states.append,
        types=["motion.started"],
    )
    await sub.run(max_connections=6)
    assert got == ["01A", "01B", "01C"]  # 01B replayed once, delivered once
    # Resumed from the last event; a reset forgets it.
    assert "Last-Event-ID" not in t.stream_headers[1]
    assert t.stream_headers[2]["Last-Event-ID"] == "01B"
    assert t.stream_headers[3]["Last-Event-ID"] == "01C"
    assert "Last-Event-ID" not in t.stream_headers[5]
    assert t.stream_params[1] == {"types": "motion.started", "format": "things"}
    # Backoff: ~1 s after the error, the server's hint after each clean end, 5 s from 429.
    assert 1.0 <= sleeps[0] < 1.2 and 5.0 in sleeps
    assert True in states and False in states
    assert sub.reconnects == 2


async def test_subscription_stops_on_auth_error() -> None:
    t = FakeTransport()
    t.streams = [(401, {"error": {"code": "unauthenticated", "message": "", "details": {}}})]

    async def sleep(_s: float) -> None:
        raise AssertionError("must not retry")

    sub = EventSubscription(client(t), lambda e: None, sleep=sleep)
    with pytest.raises(ThingsAuthError):
        await sub.run(max_connections=1)


def test_ha_mapping_table() -> None:
    things = [Thing.from_dict(t) for t in THINGS]
    plans = plan(things, HA_TABLE)
    keys = {(p.thing_id, p.rule.platform, p.rule.key) for p in plans}
    assert ("camera:1", "binary_sensor", "motion") in keys
    assert ("camera:1", "binary_sensor", "person") in keys
    assert ("camera:1", "switch", "recording") in keys
    assert ("room:7", "select", "ai_mode") in keys
    assert ("area:1", "sensor", "occupancy") in keys
    assert ("area:1", "sensor", "visitors_today") in keys
    assert ("server", "sensor", "unacknowledged_alerts") in keys
    assert ("camera:1", "event", "events") in keys
    assert ("camera:1", "camera", "camera") in keys
    # Every capability state field the table reads exists in the fixture.
    for p in plans:
        thing = next(t for t in things if t.id == p.thing_id)
        if p.rule.field:
            assert p.rule.field in thing.capabilities[p.rule.capability].state, p
    # 0.1.0-compatible unique ids.
    assert unique_id("E", "camera:1", "motion") == "E_camera_1_motion"
    assert unique_id("E", "room:7", "ai_mode") == "E_room_7_ai_mode"
    assert unique_id("E", "area:1", "occupancy") == "E_area_1_occupancy"
    assert unique_id("E", "server", "unacknowledged_alerts") == "E_unacknowledged_alerts"


def test_library_has_no_home_assistant_imports() -> None:
    import pathlib

    root = pathlib.Path(__file__).parent.parent / "custom_components" / "vigiora" / "vigiora_things"
    for f in root.glob("*.py"):
        assert "homeassistant" not in f.read_text(), f
