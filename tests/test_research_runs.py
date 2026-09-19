"""§17.1120 — detached research runs: start → run_id, tail with backlog, status,
cancel, reply; ownership; replay after the run ended."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID

import pytest
from fastapi import HTTPException

from app.authz import Principal
from app.modules import research_runs, run_broker
from app.routers import research as r

_ADMIN = Principal(identity="admin", role="admin")
_ALICE = Principal(identity="alice", role="user")
_BOB = Principal(identity="bob", role="user")


@pytest.fixture(autouse=True)
def _clean():
    research_runs.reset()
    yield
    research_runs.reset()


async def _frames(*names, delay=0.0):
    for n in names:
        if delay:
            await asyncio.sleep(delay)
        yield f"event: {n}\ndata: {{}}\n\n"


async def _drain(gen):
    return [f async for f in gen]


async def test_start_returns_a_run_id_and_the_run_is_tailable_with_backlog():
    from app.schemas import ResearchInput
    body = ResearchInput(topic="tls handshakes", depth="shallow")
    with patch.object(r, "_require_valid_models", AsyncMock()), \
         patch.object(r, "run_research", lambda **kw: _frames("research_started", "iteration_started", "research_complete")):
        out = await r.research_run_start(body, principal=_ALICE)
    run_id = out["run_id"]; UUID(run_id)
    assert out["status"] == "started" and research_runs.meta(run_id)["owner"] == "alice"
    await asyncio.sleep(0.05)                       # let the pump finish
    # the run ended → status says so; the stream replays what happened
    st = await r.research_run_status(run_id, principal=_ALICE)
    assert st["running"] is False and st["kind"] == "research"
    with patch.object(research_runs, "replay", AsyncMock(return_value=["event: research_started\ndata: {}\n\n", "event: research_complete\ndata: {}\n\n"])):
        resp = await r.research_run_stream(run_id, principal=_ALICE)
    got = await _drain(resp.body_iterator)
    assert [g.split("\n")[0] for g in got] == ["event: research_started", "event: research_complete"]


async def test_live_run_streams_backlog_then_live_and_disconnect_does_not_cancel():
    from app.schemas import ResearchInput
    with patch.object(r, "_require_valid_models", AsyncMock()), \
         patch.object(r, "run_research", lambda **kw: _frames("research_started", "iteration_started", "research_complete", delay=0.05)):
        out = await r.research_run_start(ResearchInput(topic="t", depth="shallow"), principal=_ALICE)
    run_id = out["run_id"]
    await asyncio.sleep(0.08)                        # one frame in the backlog
    assert research_runs.is_running(run_id)
    resp = await r.research_run_stream(run_id, principal=_ALICE)
    it = resp.body_iterator
    first = await it.__anext__()
    assert first.startswith("event: research_started")
    await it.aclose()                                # the client goes away…
    await asyncio.sleep(0.02)
    assert research_runs.is_running(run_id), "…and the run is untouched"
    rest = await _drain((await r.research_run_stream(run_id, principal=_ALICE)).body_iterator)
    assert rest[-1].startswith("event: research_complete")


async def test_cancel_stops_the_run():
    from app.schemas import ResearchInput
    with patch.object(r, "_require_valid_models", AsyncMock()), \
         patch.object(r, "run_research", lambda **kw: _frames("a", "b", "c", "d", delay=0.5)):
        out = await r.research_run_start(ResearchInput(topic="t", depth="shallow"), principal=_ALICE)
    run_id = out["run_id"]
    await asyncio.sleep(0.02)
    res = await r.research_run_cancel(run_id, principal=_ALICE)
    assert res["cancelled"] is True
    assert research_runs.is_running(run_id) is False


async def test_ownership_gate():
    from app.schemas import ResearchInput
    with patch.object(r, "_require_valid_models", AsyncMock()), \
         patch.object(r, "run_research", lambda **kw: _frames("a")):
        out = await r.research_run_start(ResearchInput(topic="t", depth="shallow"), principal=_ALICE)
    run_id = out["run_id"]
    for fn in (r.research_run_status, r.research_run_cancel):
        with pytest.raises(HTTPException) as e:
            await fn(run_id, principal=_BOB)
        assert e.value.status_code == 404
    assert (await r.research_run_status(run_id, principal=_ADMIN))["run_id"] == run_id
    with pytest.raises(HTTPException) as e:
        await r.research_run_status("not-a-uuid", principal=_ALICE)
    assert e.value.status_code == 422


async def test_reply_starts_a_detached_run_for_a_visible_session():
    from app.schemas import ResearchReplyInput
    sid = "0bb0df92-0000-4000-8000-000000000001"
    db = MagicMock()
    found = MagicMock(); found.first.return_value = (sid,)
    db.execute = AsyncMock(return_value=found)
    with patch.object(r, "_require_valid_models", AsyncMock()), \
         patch.object(r, "resume_research", lambda *a, **kw: _frames("research_resumed", "research_complete")):
        out = await r.research_run_reply(ResearchReplyInput(session_id=sid, reply="yes"), db=db, principal=_ALICE)
    assert out["session_id"] == sid and research_runs.meta(out["run_id"])["kind"] == "research_reply"
    missing = MagicMock(); missing.first.return_value = None
    db.execute = AsyncMock(return_value=missing)
    with patch.object(r, "_require_valid_models", AsyncMock()), pytest.raises(HTTPException) as e:
        await r.research_run_reply(ResearchReplyInput(session_id=sid, reply="yes"), db=db, principal=_BOB)
    assert e.value.status_code == 404


def test_keys_never_collide_with_execution_runs():
    assert research_runs.key("abc") == "research:abc"
    assert run_broker.get_run("research:nope") is None
