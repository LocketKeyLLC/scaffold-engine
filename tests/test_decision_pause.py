"""§17.1184 — the autonomous run stops at a decision node and asks.

Before: the executor prompted the model with "Decide P40 GPU passthrough
strategy — whole-GPU vs Docker vs LXC" like any other step and the model
chose for the operator. The approve page had promised the opposite ("blank
questions become decision points that pause the run and ask you")."""
from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import decision_pause as dp
from app.modules import execution_agent as ea

DECISION = {"node_key": "T2", "title": "Decide P40 GPU passthrough strategy", "depends_on": ["T1"],
            "prompt_template": "Operator decision: whole-GPU passthrough to a dedicated VM (suggested) vs "
                               "Docker GPU passthrough inside the VM vs LXC GPU passthrough."}


def _db(*, scalar=None, first=None, rowcount=1):
    db = AsyncMock()
    res = MagicMock()
    res.scalar.return_value = scalar
    res.mappings.return_value.first.return_value = first
    res.rowcount = rowcount
    db.execute = AsyncMock(return_value=res)
    return db


# ── framing ──────────────────────────────────────────────────────────────

def test_fallback_frame_is_the_steps_own_words():
    f = dp.fallback_frame(DECISION)
    assert f["question"] == "Decide P40 GPU passthrough strategy?" and f["options"] == [] and f["framed"] is False
    assert "whole-GPU" in f["detail"]


@pytest.mark.asyncio
async def test_frame_decision_normalises_the_tool_call_and_drops_a_foreign_suggestion():
    resp = MagicMock()
    with patch("app.model_router.tool_call", AsyncMock(return_value=resp)), \
         patch("app.utils.tool_call_args.read_tool_args", return_value={
             "question": "How should the P40 reach the AI workloads?",
             "options": [{"label": "Whole-GPU to one VM", "fit": "one AI VM", "tradeoff": "locks the card"},
                         {"label": "LXC passthrough", "fit": "many light guests", "tradeoff": "weaker isolation"},
                         {"label": ""}],
             "suggested": "Something not listed", "why": "…"}):
        f = await dp.frame_decision(DECISION, brief="home lab", upstream="")
    assert f["framed"] is True and f["question"].endswith("?")
    assert [o["label"] for o in f["options"]] == ["Whole-GPU to one VM", "LXC passthrough"]
    assert f["suggested"] == "" and f["why"] == ""      # a suggestion must name a listed option


@pytest.mark.asyncio
async def test_frame_decision_fails_soft_to_the_fallback():
    with patch("app.model_router.tool_call", AsyncMock(side_effect=RuntimeError("provider down"))):
        f = await dp.frame_decision(DECISION)
    assert f["framed"] is False and f["question"] == "Decide P40 GPU passthrough strategy?"


# ── what is waiting ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pending_decision_skips_delegated_nodes_and_requires_satisfied_deps():
    db = _db(scalar={"T2": {"by": "engine"}}, first=None)
    assert await dp.delegated_decisions(db, "j") == ["T2"]
    assert await dp.pending_decision(db, "j") is None
    sql = db.execute.await_args.args[0].text
    assert "node_type = 'decision'" in sql and "NOT (n.node_key = ANY(:delegated))" in sql and "NOT EXISTS" in sql
    assert db.execute.await_args.args[1]["delegated"] == ["T2"]


# ── park / resolve ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_park_writes_the_question_on_the_job_and_moves_it_to_awaiting_decision():
    db = _db()
    with patch("app.modules.decision_pause.transition", AsyncMock(return_value=True)) as tr:
        out = await dp.park_awaiting_decision(db, "j", DECISION, dp.fallback_frame(DECISION))
    patch_sql, params = db.execute.await_args_list[0].args
    assert "metadata = COALESCE(metadata, '{}'::jsonb) || CAST(:patch AS jsonb)" in patch_sql.text
    assert json.loads(params["patch"])["awaiting_decision"]["node_key"] == "T2"
    assert tr.await_args.kwargs["to"] == "awaiting_decision" and tr.await_args.kwargs["expected_from"] == ("running", "executing")
    assert out["status"] == "awaiting_decision" and out["node_key"] == "T2" and out["question"]
    db.commit.assert_awaited()


@pytest.mark.asyncio
async def test_resolve_records_the_choice_as_the_nodes_output_and_returns_the_job_to_executing():
    waiting = {"node_key": "T2", "options": [{"label": "Whole-GPU"}, {"label": "LXC"}]}
    db = _db(first={"status": "awaiting_decision", "metadata": {"awaiting_decision": waiting}}, rowcount=1)
    with patch("app.modules.decision_pause.transition", AsyncMock(return_value=True)) as tr:
        out = await dp.resolve_decision(db, "j", "T2", choice="Whole-GPU", note="the card is for the AI VM only")
    assert out["outcome"] == "resolved" and out["record"]["by"] == "operator"
    node_sql, node_params = db.execute.await_args_list[1].args
    assert "SET status = 'done', output_text = :out" in node_sql.text and "status = 'pending'" in node_sql.text
    assert node_params["out"].startswith("Decision (operator): Whole-GPU") and "Options considered: Whole-GPU; LXC" in node_params["out"]
    meta_sql, meta_params = db.execute.await_args_list[2].args
    assert "- 'awaiting_decision'" in meta_sql.text
    assert json.loads(meta_params["patch"])["decisions"]["T2"]["choice"] == "Whole-GPU"
    assert tr.await_args.kwargs["to"] == "executing" and tr.await_args.kwargs["expected_from"] == ("awaiting_decision",)


@pytest.mark.asyncio
async def test_resolve_delegate_marks_the_node_as_the_engines_and_leaves_it_pending():
    db = _db(first={"status": "awaiting_decision", "metadata": {"awaiting_decision": {"node_key": "T2"}}})
    with patch("app.modules.decision_pause.transition", AsyncMock(return_value=True)):
        out = await dp.resolve_decision(db, "j", "T2", choice=None, delegate=True)
    assert out["outcome"] == "delegated"
    sqls = [c.args[0].text for c in db.execute.await_args_list]
    assert not any("SET status = 'done'" in q for q in sqls)
    assert json.loads(db.execute.await_args_list[1].args[1]["patch"])["decisions"]["T2"]["by"] == "engine"


@pytest.mark.asyncio
async def test_resolve_refuses_a_job_that_is_not_waiting_on_that_node():
    db = _db(first={"status": "running", "metadata": {}})
    out = await dp.resolve_decision(db, "j", "T2", choice="x")
    assert out["outcome"] == "not_waiting" and out["current_status"] == "running"
    db = _db(first={"status": "awaiting_decision", "metadata": {"awaiting_decision": {"node_key": "T5"}}})
    out = await dp.resolve_decision(db, "j", "T2", choice="x")
    assert out["outcome"] == "not_waiting" and out["waiting_on"] == "T5"


# ── the executor stops ───────────────────────────────────────────────────

def _events(chunks):
    out = []
    for c in chunks:
        ev = [ln[7:] for ln in c.splitlines() if ln.startswith("event: ")]
        out.extend(ev)
    return out


@pytest.mark.asyncio
async def test_serial_loop_stops_at_a_decision_node_instead_of_executing_it(monkeypatch):
    monkeypatch.setattr(settings, "decision_pause_enabled", True)
    monkeypatch.setattr(settings, "parallel_execution_enabled", False)
    monkeypatch.setattr(settings, "hands_on_assist_gate_enabled", False)
    peek = AsyncMock(return_value={"node_key": "T2", "title": "Decide", "tool": "LLM", "node_type": "decision", "depends_on": []})
    exec_next = AsyncMock(return_value={"status": "done", "node_key": "T2"})
    asked = {"job_id": "j", "status": "awaiting_decision", "node_key": "T2", "question": "Which?", "options": []}
    from tests._execution_agent_shared import _make_sse_db  # the suite's DB double
    _db_, mock_session = _make_sse_db(dag_node_count=2)
    with patch("app.modules.execution_agent.async_session", mock_session), \
         patch("app.modules.execution_agent._get_job", AsyncMock(return_value={"status": "executing", "id": "j"})), \
         patch("app.modules.execution_agent._peek_next_node", peek), \
         patch("app.modules.execution_agent.execute_next_node", exec_next), \
         patch("app.modules.execution_agent._pause_for_decision", AsyncMock(return_value=asked)), \
         patch("app.modules.execution_agent._make_dag_progress_tracker", AsyncMock(return_value=None)):
        chunks = [c async for c in ea.execute_all_nodes("j")]
    assert _events(chunks)[-1] == "awaiting_decision"
    exec_next.assert_not_awaited()


@pytest.mark.asyncio
async def test_serial_loop_runs_a_decision_node_the_operator_delegated(monkeypatch):
    """_pause_for_decision returns None for a delegated node → the old path."""
    monkeypatch.setattr(settings, "decision_pause_enabled", True)
    monkeypatch.setattr(settings, "parallel_execution_enabled", False)
    monkeypatch.setattr(settings, "hands_on_assist_gate_enabled", False)
    peek = AsyncMock(side_effect=[{"node_key": "T2", "title": "Decide", "tool": "LLM", "node_type": "decision", "depends_on": []}, None])
    exec_next = AsyncMock(side_effect=[{"status": "done", "node_key": "T2", "title": "Decide", "tool": "LLM", "output": "x"},
                                       {"status": "complete", "total_nodes": 1, "passed": 1, "failed": 0}])
    from tests._execution_agent_shared import _make_sse_db
    _db_, mock_session = _make_sse_db(dag_node_count=1)
    with patch("app.modules.execution_agent.async_session", mock_session), \
         patch("app.modules.execution_agent._get_job", AsyncMock(return_value={"status": "executing", "id": "j"})), \
         patch("app.modules.execution_agent._peek_next_node", peek), \
         patch("app.modules.execution_agent.execute_next_node", exec_next), \
         patch("app.modules.execution_agent._pause_for_decision", AsyncMock(return_value=None)) as pause, \
         patch("app.modules.execution_agent._make_dag_progress_tracker", AsyncMock(return_value=None)):
        chunks = [c async for c in ea.execute_all_nodes("j")]
    pause.assert_awaited_once()
    assert "node_done" in _events(chunks)


def test_both_execute_paths_and_the_claim_are_wired():
    serial = inspect.getsource(ea.execute_all_nodes)
    assert '== "decision"' in serial and '_sse("awaiting_decision", _asked)' in serial
    parallel = inspect.getsource(ea._run_parallel_frontier)
    assert "_pause_for_decision(job_id)" in parallel and '_sse("awaiting_decision", _asked)' in parallel
    claim = inspect.getsource(ea._claim_ready_nodes_sql)
    assert "n.node_type IS DISTINCT FROM 'decision'" in claim and "n.node_key = ANY(:delegated)" in claim
    peek = inspect.getsource(ea._peek_next_node)
    assert "node_type" in peek


def test_pause_check_failure_is_logged_as_an_error_not_swallowed_quietly():
    src = inspect.getsource(ea._pause_for_decision)
    assert 'logger.error("decision_pause_check_failed' in src


# ── the vocabulary is complete ───────────────────────────────────────────

def test_the_status_is_in_every_vocabulary():
    from app.modules.job_state import JOB_STATUSES
    from app.schemas import JOB_STATUSES as SCHEMA_STATUSES
    from app.modules.recovery import NEXT_ACTIONS
    from app import sse_events
    assert "awaiting_decision" in JOB_STATUSES and "awaiting_decision" in SCHEMA_STATUSES
    assert NEXT_ACTIONS["awaiting_decision"][0]["action"] == "decide"
    assert sse_events.AWAITING_DECISION == "awaiting_decision"
