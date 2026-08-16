"""Sync + async ``client.assist`` resource and ``aiter_assist_handoff`` SSE.

Mirrors the patterns in test_typed_methods (sync), test_async_typed_methods
(async), and test_sse (MockTransport-backed streams).
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from scaffold_client import AsyncClient, Client


def _resp(status: int = 200, payload: dict | None = None) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.text = ""
    r.json = MagicMock(return_value=payload if payload is not None else {})
    return r


def _last_call(mock):
    return mock.call_args.args, mock.call_args.kwargs


# ---------------------------------------------------------------------------
# Sync client.assist.*
# ---------------------------------------------------------------------------


@pytest.fixture
def client():
    with Client("http://example.com", api_key="k") as c:
        yield c


def test_assist_start_minimal(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"session_id": "s"})) as m:
        client.assist.start("job-1")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/assist/start")
    assert kwargs["json"] == {"job_id": "job-1"}


def test_assist_start_with_policies(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.assist.start(
            "job-1", handoff_policy="auto_on_skip", replan_policy="selective",
        )
    _, kwargs = _last_call(m)
    assert kwargs["json"] == {
        "job_id": "job-1",
        "handoff_policy": "auto_on_skip",
        "replan_policy": "selective",
    }


def test_assist_get(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.assist.get("sess-1")
    args, _ = _last_call(m)
    assert args == ("GET", "/assist/sess-1")


def test_assist_next(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.assist.next("sess-1")
    args, _ = _last_call(m)
    assert args == ("GET", "/assist/sess-1/next")


def test_assist_submit_default_action(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.assist.submit("sess-1", "T2", output="all good")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/assist/sess-1/submit")
    assert kwargs["json"]["action"] == "submit"
    assert kwargs["json"]["output"] == "all good"
    assert kwargs["json"]["evidence_kind"] == "text"


def test_assist_submit_passes_evidence_meta(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.assist.submit(
            "sess-1", "T2",
            output="diff",
            evidence_kind="file_diff",
            evidence_meta={"path": "src/foo.py"},
            friction_note="took 3 attempts",
        )
    _, kwargs = _last_call(m)
    assert kwargs["json"]["evidence_kind"] == "file_diff"
    assert kwargs["json"]["evidence_meta"] == {"path": "src/foo.py"}
    assert kwargs["json"]["friction_note"] == "took 3 attempts"


def test_assist_skip_is_submit_shorthand(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.assist.skip("sess-1", "T2")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/assist/sess-1/submit")
    assert kwargs["json"]["action"] == "skip"
    assert kwargs["json"]["evidence_kind"] == "none"


def test_assist_pause(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.assist.pause("sess-1")
    args, _ = _last_call(m)
    assert args == ("POST", "/assist/sess-1/pause")


def test_assist_resume(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.assist.resume("sess-1")
    args, _ = _last_call(m)
    assert args == ("POST", "/assist/sess-1/resume")


def test_assist_abandon(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.assist.abandon("sess-1")
    args, _ = _last_call(m)
    assert args == ("DELETE", "/assist/sess-1")


def test_assist_add_friction(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.assist.add_friction("sess-1", "T2", "the docs lied")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/assist/sess-1/friction")
    assert kwargs["json"] == {"node_key": "T2", "note": "the docs lied"}


def test_assist_list_friction(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.assist.list_friction("sess-1")
    args, _ = _last_call(m)
    assert args == ("GET", "/assist/sess-1/friction")


def test_assist_resource_has_stable_identity(client):
    assert client.assist is client.assist


# ---------------------------------------------------------------------------
# Async client.assist.*
# ---------------------------------------------------------------------------


@pytest.fixture
async def aclient():
    async with AsyncClient("http://example.com", api_key="k") as c:
        yield c


async def test_async_assist_start(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={"session_id": "s"})) as m:
        await aclient.assist.start("job-1", replan_policy="disabled")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/assist/start")
    assert kwargs["json"]["replan_policy"] == "disabled"


async def test_async_assist_next(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.assist.next("sess-1")
    args, _ = _last_call(m)
    assert args == ("GET", "/assist/sess-1/next")


async def test_async_assist_submit(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.assist.submit("sess-1", "T2", output="ok")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/assist/sess-1/submit")
    assert kwargs["json"]["output"] == "ok"


async def test_async_assist_skip_is_shorthand(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.assist.skip("sess-1", "T2")
    _, kwargs = _last_call(m)
    assert kwargs["json"]["action"] == "skip"


async def test_async_assist_abandon(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.assist.abandon("sess-1")
    args, _ = _last_call(m)
    assert args == ("DELETE", "/assist/sess-1")


async def test_async_assist_resource_has_stable_identity(aclient):
    assert aclient.assist is aclient.assist


# ---------------------------------------------------------------------------
# aiter_assist_handoff (SSE)
# ---------------------------------------------------------------------------


def _sse_body(frames: list[tuple[str, dict]]) -> bytes:
    return "".join(
        f"event: {ev}\ndata: {json.dumps(d)}\n\n" for ev, d in frames
    ).encode("utf-8")


def _aclient_with_transport(transport: httpx.MockTransport) -> AsyncClient:
    c = AsyncClient("http://example.com")
    c._http = httpx.AsyncClient(
        base_url="http://example.com",
        transport=transport,
        headers=c._http.headers,
        timeout=c._http.timeout,
    )
    return c


async def test_aiter_assist_handoff_yields_events_and_sends_body():
    captured = {}

    def handler(req: httpx.Request) -> httpx.Response:
        captured["path"] = req.url.path
        captured["body"] = json.loads(req.content)
        return httpx.Response(
            200,
            content=_sse_body([
                ("node_started", {"node_key": "T2"}),
                ("node_completed", {"node_key": "T2", "status": "done"}),
            ]),
            headers={"content-type": "text/event-stream"},
        )

    c = _aclient_with_transport(httpx.MockTransport(handler))
    try:
        events = [
            e async for e in c.aiter_assist_handoff("sess-1", "T2", mode="single")
        ]
    finally:
        await c.aclose()

    assert captured["path"] == "/assist/sess-1/handoff"
    assert captured["body"] == {"node_key": "T2", "mode": "single"}
    assert [e["event"] for e in events] == ["node_started", "node_completed"]


async def test_aiter_assist_handoff_propagates_409():
    """A non-active session should bubble up as the SDK's typed error."""
    from scaffold_client import ScaffoldError

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            content=b'{"detail": "session status \'paused\' cannot handoff"}',
            headers={"content-type": "application/json"},
        )

    c = _aclient_with_transport(httpx.MockTransport(handler))
    try:
        with pytest.raises(ScaffoldError):
            async for _ in c.aiter_assist_handoff("sess-1", "T2"):
                pass
    finally:
        await c.aclose()
