"""§17.1036 — the approval chain is the server's, detached and idempotent.

Live (§17.1035 E2E): the SPA awaited /ideate/confirm and only then posted
/dag; the page closed between the two, and the job sat in `planning` with no
plan and nothing able to restart it. These pin the chain's phase selection,
its idempotency, and the resume sweep — with every phase function and the
job-state reader patched, so no DB or model is touched.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock

import pytest

from app.modules import advance_chain as ac

pytestmark = pytest.mark.asyncio


def _states(*seq):
    """A job-state reader that returns successive snapshots (last one sticks)."""
    it = list(seq)

    async def _read(job_id):
        return it.pop(0) if len(it) > 1 else it[0]
    return _read


@pytest.fixture
def phases(monkeypatch):
    calls = {"research": [], "dag": [], "assist": [], "marks": []}

    async def research(job_id, db, **kw):
        calls["research"].append(kw.get("user_feedback")); return {"status": "planning"}

    async def dag(job_id, db, **kw):
        calls["dag"].append(job_id); return {"task_count": 3}

    async def assist(*, job_id, db, **kw):
        calls["assist"].append(job_id); return {"session_id": "s1"}

    async def mark(job_id, **fields):
        calls["marks"].append(fields.get("phase"))

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(ac, "async_session", lambda: _Sess())
    monkeypatch.setattr(ac, "_mark", mark)
    import app.modules.ideation_workflow as iw, app.modules.dag_generator as dg, app.modules.assist_agent as aa
    monkeypatch.setattr(iw, "research_and_compile", research)
    monkeypatch.setattr(dg, "generate_dag", dag)
    monkeypatch.setattr(aa, "start_assist_session", assist)
    ac._CHAINS.clear()
    return calls


async def test_from_the_gate_the_chain_runs_research_then_plan_then_assist(phases, monkeypatch):
    monkeypatch.setattr(ac, "_job_state", _states(
        {"status": "awaiting_confirmation", "node_count": 0, "has_session": False, "metadata": {}},
        {"status": "planning", "node_count": 0, "has_session": False, "metadata": {}},
        {"status": "executing", "node_count": 3, "has_session": False, "metadata": {}},
    ))
    await ac._run_chain("j1", feedback="Q: x\nA: y", model_overrides=None, assist=True,
                        execute=False, push_to_github=False)
    assert phases["research"] == ["Q: x\nA: y"]
    assert phases["dag"] == ["j1"] and phases["assist"] == ["j1"]
    assert phases["marks"][-1] == "done"


async def test_a_stranded_planning_job_skips_research_and_only_plans(phases, monkeypatch):
    """The live case: research done, page closed, no plan."""
    monkeypatch.setattr(ac, "_job_state", _states(
        {"status": "planning", "node_count": 0, "has_session": False, "metadata": {}},
        {"status": "executing", "node_count": 17, "has_session": False, "metadata": {}},
    ))
    await ac._run_chain("j2", feedback=None, model_overrides=None, assist=False,
                        execute=False, push_to_github=False)
    assert phases["research"] == [] and phases["dag"] == ["j2"] and phases["assist"] == []


async def test_a_planned_job_is_not_planned_again(phases, monkeypatch):
    monkeypatch.setattr(ac, "_job_state", _states(
        {"status": "executing", "node_count": 17, "has_session": True, "metadata": {}}))
    await ac._run_chain("j3", feedback=None, model_overrides=None, assist=True,
                        execute=False, push_to_github=False)
    assert phases["research"] == [] and phases["dag"] == [] and phases["assist"] == []
    assert phases["marks"][-1] == "done"


async def test_a_research_error_stops_the_chain_and_is_recorded(phases, monkeypatch):
    import app.modules.ideation_workflow as iw

    async def bad(job_id, db, **kw):
        return {"error": "model unavailable", "http_status": 503}
    monkeypatch.setattr(iw, "research_and_compile", bad)
    monkeypatch.setattr(ac, "_job_state", _states(
        {"status": "awaiting_confirmation", "node_count": 0, "has_session": False, "metadata": {}}))
    await ac._run_chain("j4", feedback=None, model_overrides=None, assist=True,
                        execute=False, push_to_github=False)
    assert phases["dag"] == [] and phases["marks"][-1] == "error"


async def test_start_is_idempotent_while_a_chain_is_live(phases, monkeypatch):
    monkeypatch.setattr(ac, "_job_state", _states(
        {"status": "awaiting_confirmation", "node_count": 0, "has_session": False, "metadata": {}}))
    gate = asyncio.Event()

    async def slow(job_id, **kw):
        await gate.wait()
    monkeypatch.setattr(ac, "_run_chain", slow)
    async def _fresh(job_id):  # a NEW dict per call — the code mutates what it returns
        return {"job_id": job_id, "chain": "running"}
    monkeypatch.setattr(ac, "chain_state", _fresh)
    first = await ac.start_advance_chain("j5", assist=True)
    second = await ac.start_advance_chain("j5", assist=True)
    assert first["started"] is True and second["started"] is False
    gate.set(); await asyncio.sleep(0)


async def test_start_does_nothing_when_nothing_is_left_to_do(phases, monkeypatch):
    monkeypatch.setattr(ac, "_job_state", _states(
        {"status": "executing", "node_count": 5, "has_session": True, "metadata": {}}))
    monkeypatch.setattr(ac, "chain_state", AsyncMock(return_value={"job_id": "j6", "chain": "idle"}))
    out = await ac.start_advance_chain("j6", assist=True)
    assert out["started"] is False and "j6" not in ac._CHAINS


async def test_resume_sweep_starts_stranded_jobs_and_leaves_fresh_markers_alone(phases, monkeypatch):
    from datetime import datetime, timedelta, timezone
    fresh = (datetime.now(timezone.utc) - timedelta(seconds=30)).isoformat()
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
    rows = [
        {"id": "s-old", "metadata": {"advance_chain": {"phase": "planning", "updated_at": old, "assist": True}}},
        {"id": "s-fresh", "metadata": {"advance_chain": {"phase": "planning", "updated_at": fresh}}},
        {"id": "s-none", "metadata": {}},
    ]

    class _Res:
        def mappings(self): return self
        def all(self): return rows

    class _Sess:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, *a, **k): return _Res()
    monkeypatch.setattr(ac, "async_session", lambda: _Sess())
    started = []

    async def fake_start(job_id, **kw):
        started.append((job_id, kw.get("assist"), kw.get("source")))
        return {"started": True}
    monkeypatch.setattr(ac, "start_advance_chain", fake_start)
    resumed = await ac.resume_stranded_planning(older_than_minutes=3)
    assert resumed == ["s-old", "s-none"]
    assert started == [("s-old", True, "resume"), ("s-none", False, "resume")]


def test_the_spa_no_longer_runs_the_chain_itself():
    """Source-scan: approvals.js must post /jobs/{id}/approve and must not
    await /ideate/confirm or /dag from the browser any more."""
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "app/ui/static/views/approvals.js"
    js = src.read_text(encoding="utf-8")
    assert "/approve`" in js and "waitForChain" in js
    assert 'api.post("/ideate/confirm"' not in js and 'api.post("/dag"' not in js


def test_the_flow_guide_offers_resume_for_a_stranded_planning_job():
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[1] / "app/ui/static/views/flow_guide.js"
    js = src.read_text(encoding="utf-8")
    assert "Resume planning" in js and "resume: true" in js and "/approve`" in js
