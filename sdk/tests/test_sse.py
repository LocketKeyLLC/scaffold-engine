"""Tests for the SSE parser + ``AsyncClient`` streaming endpoints.

The parser unit tests exercise edge cases (empty events, multi-line data,
heartbeats, no trailing blank line) without involving httpx.

The streaming tests use ``httpx.MockTransport`` to feed canned SSE bytes
into a real ``AsyncClient``, which is the closest unit-test surface to
the orchestrator without bringing it up.
"""
from __future__ import annotations

import json
from typing import AsyncIterator

import httpx
import pytest

from scaffold_client import AsyncClient, NotFoundError, OrchestratorError
from scaffold_client._sse import parse_sse_lines


# --------------------------------------------------------------------------
# Parser unit tests
# --------------------------------------------------------------------------


async def _alines(items: list[str]) -> AsyncIterator[str]:
    for item in items:
        yield item


async def _collect(agen):
    return [event async for event in agen]


async def test_parser_emits_one_event_per_blank_line():
    lines = [
        "event: search_complete",
        'data: {"results": 7}',
        "",
        "event: convergence",
        'data: {"reason": "stable"}',
        "",
    ]
    events = await _collect(parse_sse_lines(_alines(lines)))
    assert events == [
        {"event": "search_complete", "data": {"results": 7}},
        {"event": "convergence", "data": {"reason": "stable"}},
    ]


async def test_parser_filters_heartbeats_by_default():
    lines = [
        ": keepalive",
        ": keepalive",
        "event: tick",
        "data: {}",
        "",
    ]
    events = await _collect(parse_sse_lines(_alines(lines)))
    assert events == [{"event": "tick", "data": {}}]


async def test_parser_surfaces_heartbeats_when_requested():
    lines = [": keepalive", "event: tick", "data: {}", ""]
    events = await _collect(
        parse_sse_lines(_alines(lines), include_heartbeats=True)
    )
    assert events[0] == {"event": "heartbeat", "data": None}
    assert events[1] == {"event": "tick", "data": {}}


async def test_parser_handles_data_only_event():
    """Streams that omit an `event:` line should default to `message`."""
    lines = ['data: {"raw": true}', ""]
    events = await _collect(parse_sse_lines(_alines(lines)))
    assert events == [{"event": "message", "data": {"raw": True}}]


async def test_parser_passes_through_non_json_payload():
    lines = ["event: log", "data: hello world", ""]
    events = await _collect(parse_sse_lines(_alines(lines)))
    assert events == [{"event": "log", "data": "hello world"}]


async def test_parser_flushes_trailing_event_without_blank_line():
    """Some streams terminate without a final empty line; flush anyway."""
    lines = ["event: tail", 'data: {"k": 1}']
    events = await _collect(parse_sse_lines(_alines(lines)))
    assert events == [{"event": "tail", "data": {"k": 1}}]


async def test_parser_concatenates_multi_line_data():
    lines = [
        "event: chunk",
        "data: line1",
        "data: line2",
        "",
    ]
    events = await _collect(parse_sse_lines(_alines(lines)))
    assert events == [{"event": "chunk", "data": "line1\nline2"}]


# --------------------------------------------------------------------------
# AsyncClient streaming endpoints (httpx MockTransport)
# --------------------------------------------------------------------------


def _sse_body(frames: list[tuple[str, dict]]) -> bytes:
    """Build a properly formatted SSE wire body for a list of (event, data) tuples."""
    chunks = []
    for event, data in frames:
        chunks.append(f"event: {event}\ndata: {json.dumps(data)}\n\n")
    return "".join(chunks).encode("utf-8")


def _streaming_transport(body: bytes, *, status: int = 200) -> httpx.MockTransport:
    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            status,
            content=body,
            headers={"content-type": "text/event-stream"},
        )
    return httpx.MockTransport(handler)


def _aclient_with_transport(transport: httpx.MockTransport) -> AsyncClient:
    """Build an AsyncClient backed by a MockTransport in place of the real socket."""
    c = AsyncClient("http://example.com")
    # Swap the underlying httpx client to one with the mock transport. This is
    # finer-grained than monkey-patching because the SDK still wires headers
    # and timeouts as a normal user would.
    c._http = httpx.AsyncClient(
        base_url="http://example.com",
        transport=transport,
        headers=c._http.headers,
        timeout=c._http.timeout,
    )
    return c


async def test_aiter_research_yields_typed_events():
    body = _sse_body([
        ("iteration_started", {"i": 1}),
        ("search_complete", {"results": 7}),
        ("convergence", {"reason": "stable"}),
    ])
    c = _aclient_with_transport(_streaming_transport(body))
    try:
        events = [e async for e in c.aiter_research("kubernetes")]
    finally:
        await c.aclose()

    assert [e["event"] for e in events] == [
        "iteration_started", "search_complete", "convergence",
    ]
    assert events[1]["data"] == {"results": 7}


async def test_aiter_execute_all_yields_typed_events():
    body = _sse_body([
        ("dag_generated", {"job_id": "abc", "num_nodes": 3}),
        ("node_complete", {"node_key": "T1", "status": "done"}),
    ])
    c = _aclient_with_transport(_streaming_transport(body))
    try:
        events = [e async for e in c.aiter_execute_all("abc")]
    finally:
        await c.aclose()

    assert events[0]["event"] == "dag_generated"
    assert events[1]["data"]["node_key"] == "T1"


async def test_aiter_research_reply_sends_session_id_and_reply():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["url"] = str(req.url)
        captured["body"] = json.loads(req.content)
        return httpx.Response(200, content=_sse_body([("done", {})]),
                              headers={"content-type": "text/event-stream"})

    c = _aclient_with_transport(httpx.MockTransport(handler))
    try:
        async for _ in c.aiter_research_reply("sess-1", "yes"):
            pass
    finally:
        await c.aclose()

    assert captured["url"].endswith("/research/reply")
    assert captured["body"] == {"session_id": "sess-1", "reply": "yes"}


async def test_streaming_404_raises_not_found_error():
    c = _aclient_with_transport(_streaming_transport(
        b'{"detail": "missing"}', status=404,
    ))
    try:
        with pytest.raises(NotFoundError):
            async for _ in c.aiter_research("x"):
                pass
    finally:
        await c.aclose()


async def test_streaming_500_raises_orchestrator_error():
    c = _aclient_with_transport(_streaming_transport(
        b'{"detail": "boom"}', status=500,
    ))
    try:
        with pytest.raises(OrchestratorError):
            async for _ in c.aiter_execute_all("abc"):
                pass
    finally:
        await c.aclose()


async def test_aiter_research_pdf_uploads_bytes_as_multipart():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["path"] = req.url.path
        captured["query"] = dict(req.url.params)
        captured["content_type"] = req.headers.get("content-type", "")
        captured["body_bytes"] = req.content
        return httpx.Response(200, content=_sse_body([("ingested", {})]),
                              headers={"content-type": "text/event-stream"})

    c = _aclient_with_transport(httpx.MockTransport(handler))
    try:
        async for _ in c.aiter_research_pdf(b"%PDF-1.4 fake content", filename="foo.pdf"):
            pass
    finally:
        await c.aclose()

    assert captured["path"] == "/research/pdf"
    assert captured["query"] == {"extractor": "auto"}
    assert captured["content_type"].startswith("multipart/form-data")
    assert b"foo.pdf" in captured["body_bytes"]
    assert b"%PDF-1.4 fake content" in captured["body_bytes"]


async def test_aiter_research_includes_heartbeats_when_opted_in():
    """Heartbeat comments interleaved with real frames should surface when asked."""
    body = (
        b": keepalive\n\n"
        b"event: tick\ndata: {}\n\n"
        b": keepalive\n\n"
    )
    c = _aclient_with_transport(_streaming_transport(body))
    try:
        events = [e async for e in c.aiter_research("x", include_heartbeats=True)]
    finally:
        await c.aclose()

    assert {"event": "heartbeat", "data": None} in events
    assert {"event": "tick", "data": {}} in events
