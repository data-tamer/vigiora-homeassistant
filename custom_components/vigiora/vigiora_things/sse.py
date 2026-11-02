"""A small Server-Sent Events parser (WHATWG rules, simplified)."""

from __future__ import annotations

from collections.abc import AsyncIterable, AsyncIterator
from dataclasses import dataclass


@dataclass(slots=True)
class SSEFrame:
    """One frame of an event stream (comment=True for a ': …' line)."""

    event: str | None
    id: str | None
    data: str | None
    comment: bool = False


async def parse_sse(lines: AsyncIterable[bytes]) -> AsyncIterator[SSEFrame]:
    """Parse a text/event-stream body (an async iterable of lines) into frames."""
    event: str | None = None
    ev_id: str | None = None
    data: list[str] = []
    async for raw in lines:
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
