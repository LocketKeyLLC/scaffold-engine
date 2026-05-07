"""SSE frame parser for streamed orchestrator endpoints.

The orchestrator emits text/event-stream where each frame looks like::

    event: <event_name>
    data: <json_payload>

with frames separated by an empty line. ``_sse_with_disconnect_watch``
also interleaves SSE comment lines (``: keepalive``) every ~2s while the
generator is idle so client disconnect is detected promptly.

``parse_sse_lines`` consumes an async iterator of lines (typically
``httpx.Response.aiter_lines``) and yields events as dicts:

    {"event": "search_complete", "data": {...}}

Heartbeat comment lines surface as ``{"event": "heartbeat", "data": None}``
when ``include_heartbeats=True``; otherwise they are filtered.

The parser intentionally does **not** raise on malformed JSON — payloads
that fail ``json.loads`` are passed through as raw strings so the caller
can decide how to recover.
"""
from __future__ import annotations

import json
from typing import Any, AsyncIterator


def _make_event(event_type: str | None, data_lines: list[str]) -> dict[str, Any]:
    """Stitch a captured event_type + data line buffer into a frame dict."""
    raw = "\n".join(data_lines)
    if not raw:
        payload: Any = None
    else:
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            payload = raw
    return {"event": event_type or "message", "data": payload}


async def parse_sse_lines(
    lines: AsyncIterator[str],
    *,
    include_heartbeats: bool = False,
) -> AsyncIterator[dict[str, Any]]:
    """Parse an SSE byte/line stream into event dicts."""
    event_type: str | None = None
    data_lines: list[str] = []

    async for line in lines:
        if line == "":
            # Empty line = frame boundary; emit if we've buffered anything.
            if event_type is not None or data_lines:
                yield _make_event(event_type, data_lines)
            event_type = None
            data_lines = []
            continue

        if line.startswith(":"):
            # SSE comment — keepalive heartbeats use this form.
            if include_heartbeats:
                yield {"event": "heartbeat", "data": None}
            continue

        if line.startswith("event:"):
            event_type = line[len("event:"):].strip()
        elif line.startswith("data:"):
            # SSE allows ``data:`` and ``data: ``; lstrip removes the
            # optional space without eating intentional leading whitespace
            # (which would still be present after ``data:``).
            payload_fragment = line[len("data:"):]
            if payload_fragment.startswith(" "):
                payload_fragment = payload_fragment[1:]
            data_lines.append(payload_fragment)
        # Other field names (id:, retry:) are uncommon for our use; ignore.

    # Some streams terminate without a trailing blank line — flush.
    if event_type is not None or data_lines:
        yield _make_event(event_type, data_lines)
