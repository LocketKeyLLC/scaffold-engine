"""§17.624 — unit tests for the hands-on assist gate.

The gate parks a job whose DAG is predominantly non-autonomously-executable
(Shell steps with no shell backend, or human steps) in 'awaiting_assist' with
the plan, instead of fabricating runbook "done" output and rolling up to a
misleading 'completed'. End-to-end coverage is the live smoke; these tests pin
the classification + parking choreography and the umbrella roll-up propagation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import settings
from app.modules import execution_agent, decomposition


def _result(mappings_all=None, first=None):
    r = MagicMock()
    m = MagicMock()
    m.all.return_value = mappings_all or []
    m.first.return_value = None
    r.mappings.return_value = m
    r.first.return_value = first
    return r


def _tools(*tools):
    return _result(mappings_all=[{"tool": t} for t in tools])


# ── node predicate ───────────────────────────────────────────────────────────

def test_node_is_nonexecutable_human_and_shell(monkeypatch):
    monkeypatch.setattr(settings, "shell_tool_enabled", False)
    assert execution_agent._node_is_nonexecutable("human") is True
    assert execution_agent._node_is_nonexecutable("human_review") is True
    assert execution_agent._node_is_nonexecutable("Shell") is True   # no backend
    assert execution_agent._node_is_nonexecutable("LLM") is False
    assert execution_agent._node_is_nonexecutable(None) is False


def test_shell_is_executable_when_backend_wired(monkeypatch):
    monkeypatch.setattr(settings, "shell_tool_enabled", True)
    # With a real backend, Shell is executable; only human stays non-exec.
    assert execution_agent._node_is_nonexecutable("Shell") is False
    assert execution_agent._node_is_nonexecutable("human") is True


# ── classification ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_classify_hands_on_majority_shell(monkeypatch):
    """The Firewall component shape: 5 Shell + 2 LLM → hands-on."""
    monkeypatch.setattr(settings, "shell_tool_enabled", False)
    monkeypatch.setattr(settings, "hands_on_assist_gate_threshold", 0.5)
    db = AsyncMock()
    db.execute.return_value = _tools(
        "LLM", "Shell", "Shell", "Shell", "Shell", "Shell", "LLM"
    )
    cls = await execution_agent._classify_dag_executability(db, "job-1")
    assert cls == {"total": 7, "nonexec": 5, "hands_on": True}


@pytest.mark.asyncio
async def test_classify_not_hands_on_single_shell(monkeypatch):
    """A mostly-LLM DAG with one Shell step still runs autonomously."""
    monkeypatch.setattr(settings, "shell_tool_enabled", False)
    monkeypatch.setattr(settings, "hands_on_assist_gate_threshold", 0.5)
    db = AsyncMock()
    db.execute.return_value = _tools("LLM", "LLM", "LLM", "Shell")
    cls = await execution_agent._classify_dag_executability(db, "job-1")
    assert cls["hands_on"] is False
    assert cls["nonexec"] == 1


@pytest.mark.asyncio
async def test_classify_empty_dag_not_hands_on():
    db = AsyncMock()
    db.execute.return_value = _tools()
    cls = await execution_agent._classify_dag_executability(db, "job-1")
    assert cls == {"total": 0, "nonexec": 0, "hands_on": False}


# ── parking ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_park_job_awaiting_assist_sets_status_and_plan(monkeypatch):
    async def _fake_plan(job_id, db, *, nonexec_count, total):
        return f"PLAN {nonexec_count}/{total}"
    monkeypatch.setattr(
        "app.modules.execution_compile.compile_awaiting_assist_plan", _fake_plan
    )
    db = AsyncMock()
    # 1st execute = claim UPDATE ... RETURNING id (owns the row); 2nd = plan write.
    db.execute.side_effect = [_result(first=("job-1",)), _result()]
    out = await execution_agent._park_job_awaiting_assist(
        db, "job-1", {"total": 7, "nonexec": 5, "hands_on": True}
    )
    assert out["status"] == "awaiting_assist"
    assert out["parked"] is True
    assert out["hands_on_nodes"] == 5 and out["total_nodes"] == 7
    assert db.commit.await_count == 1
    # The status-claim UPDATE must target awaiting_assist.
    claim_sql = str(db.execute.await_args_list[0].args[0])
    assert "awaiting_assist" in claim_sql


@pytest.mark.asyncio
async def test_park_job_lost_ownership_no_write(monkeypatch):
    """If the claim UPDATE matches no row (a racing owner moved it), park
    reports unparked and does NOT write a plan or commit."""
    db = AsyncMock()
    db.execute.side_effect = [_result(first=None)]  # claim returns nothing
    out = await execution_agent._park_job_awaiting_assist(
        db, "job-1", {"total": 7, "nonexec": 5, "hands_on": True}
    )
    assert out["parked"] is False and out["status"] == "unknown"
    assert db.commit.await_count == 0


# ── umbrella roll-up propagation ─────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rollup_umbrella_awaiting_assist_propagates(monkeypatch):
    """A parked child makes the umbrella 'awaiting_assist', not 'completed'."""
    async def _fake_compile(db, umbrella_id):
        return "UMBRELLA DELIVERABLE"
    monkeypatch.setattr(decomposition, "_compile_umbrella_deliverable", _fake_compile)
    db = AsyncMock()
    # roll-up count query: 3 children, all terminal, 2 completed, 1 awaiting.
    count_row = {"total": 3, "terminal": 3, "done": 2, "awaiting": 1}
    db.execute.side_effect = [
        _mapping_result(count_row),      # SELECT count(*) FILTER (...)
        _result(first=("umbrella-1",)),  # UPDATE ... WHERE status='aggregating' RETURNING id
    ]
    await decomposition._rollup_umbrella(db, "umbrella-1")
    # The UPDATE (2nd execute) must set status to awaiting_assist.
    params = db.execute.await_args_list[1].args[1]
    assert params["s"] == "awaiting_assist"


def _mapping_result(row: dict):
    r = MagicMock()
    m = MagicMock()
    m.first.return_value = row
    r.mappings.return_value = m
    r.first.return_value = None
    return r
