"""§17.1190 — handing the REST of a plan to the engine, from the walkthrough.

`handoff_step(mode="all_remaining")` has routed into `execute_all_nodes` since
§17.856, and that path now stops to ask at a decision (§17.1184) and at every
step that changes a machine (§17.1186). The operator could not reach it: the
SPA only ever posted `mode:"single"`, and `auto_all_remaining` is a POLICY
fixed at session start that fires on a skip. These cover the control that
closes the gap — and, as much, the answer it gives when pressing it would
achieve nothing.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_handoff as ah


def _db(session_row, pending_rows):
    """A db whose two SELECTs answer with the session and the pending nodes."""
    db = MagicMock()
    calls = {"n": 0}

    async def execute(stmt, params=None):
        calls["n"] += 1
        res = MagicMock()
        sql = str(stmt)
        if "FROM assist_sessions" in sql:
            res.mappings.return_value.first.return_value = session_row
        elif "FROM dag_nodes" in sql:
            res.mappings.return_value.all.return_value = pending_rows
        else:
            res.rowcount = 1
        return res

    db.execute = AsyncMock(side_effect=execute)
    db.commit = AsyncMock()
    return db


SESSION = {"id": "s1", "job_id": "j1", "status": "active", "current_node_key": "ADD65"}
HANDS_ON = {"node_key": "N1", "title": "Start the container",
            "description": "Run `pct start 111`. Done when `pct status 111` reports running.",
            "prompt_template": "", "tool": "LLM", "node_type": "task"}
WRITING = {"node_key": "N2", "title": "Write the summary", "description": "Draft the summary section.",
           "prompt_template": "", "tool": "LLM", "node_type": "task"}
DECISION = {"node_key": "D1", "title": "Pick the pool", "description": "NVMe or NAS.",
            "prompt_template": "", "tool": "LLM", "node_type": "decision"}


async def _preview(session=SESSION, pending=(HANDS_ON, WRITING, DECISION), *, channel=None, gate=None):
    gate = gate or {"total": 3, "nonexec": 0, "hands_on": False, "by_text": 0}
    with patch("app.modules.supervised_runs.channel", new=AsyncMock(return_value=channel)), \
         patch("app.modules.execution_agent._classify_dag_executability", new=AsyncMock(return_value=gate)):
        return await ah.preview_all_remaining(_db(session, list(pending)), "s1")


@pytest.mark.asyncio
async def test_the_preview_counts_what_is_left_and_what_touches_a_machine():
    spec = MagicMock(); spec.name = "pve-runner"
    pre = await _preview(channel=(spec, {"allow": ["pct start", "qm set"], "sudo": True}))
    assert pre["remaining"] == 3 and pre["hands_on"] == 1 and pre["decisions"] == 1
    assert pre["channel_open"] is True and pre["runner"] == "pve-runner"
    assert pre["allow"] == ["pct start", "qm set"]
    assert pre["can_start"] is True and pre["blocker"] == ""


@pytest.mark.asyncio
async def test_it_refuses_when_the_gate_would_hand_the_plan_straight_back():
    """The §17.624 park is the whole point of the check: with a hands-on plan
    and no open channel, `execute_all_nodes` parks the job back to this
    walkthrough — so the control must say that instead of doing it."""
    pre = await _preview(channel=None, gate={"total": 131, "nonexec": 123, "hands_on": True, "by_text": 75})
    assert pre["can_start"] is False
    assert "hand this plan straight back" in pre["blocker"]
    assert "123 of 131" in pre["blocker"]
    assert "Capabilities" in pre["blocker"], pre["blocker"]


@pytest.mark.asyncio
async def test_it_refuses_a_finished_or_inactive_walkthrough():
    pre = await _preview(pending=())
    assert pre["can_start"] is False and "already done or skipped" in pre["blocker"]
    pre = await _preview(session={**SESSION, "status": "completed"})
    assert pre["can_start"] is False and "not active" in pre["blocker"]


@pytest.mark.asyncio
async def test_an_unknown_session_is_a_value_error_not_a_crash():
    with pytest.raises(ValueError):
        await _preview(session=None)


@pytest.mark.asyncio
async def test_starting_hands_every_step_over_and_detaches_the_run():
    """The SSE /handoff path calls execute_all_nodes INSIDE the response
    generator, so a closed tab would cancel a run of this size (§17.1007).
    This uses run_broker, the seam /jobs/{id}/decide uses."""
    spec = MagicMock(); spec.name = "pve-runner"
    db = _db(SESSION, [HANDS_ON, WRITING])
    started = {}

    def fake_start(job_id, factory):
        started["job_id"] = job_id
        started["callable"] = callable(factory)
        return MagicMock()

    with patch("app.modules.supervised_runs.channel", new=AsyncMock(return_value=(spec, {"allow": ["pct start"]}))), \
         patch("app.modules.execution_agent._classify_dag_executability",
               new=AsyncMock(return_value={"total": 2, "nonexec": 0, "hands_on": False, "by_text": 0})), \
         patch("app.modules.run_broker.start", new=fake_start), \
         patch("app.modules.assist_handoff.async_session") as sess_cm, \
         patch("app.modules.assist_handoff.transition", new=AsyncMock(return_value=True)) as tr:
        sess_cm.return_value.__aenter__ = AsyncMock(return_value=MagicMock(commit=AsyncMock()))
        sess_cm.return_value.__aexit__ = AsyncMock(return_value=False)
        res = await ah.start_all_remaining(db, "s1")

    assert res["started"] is True and started["job_id"] == "j1" and started["callable"]
    assert tr.await_args.kwargs["to"] == "executing"
    handed = [c for c in db.execute.await_args_list if "handed_off" in str(c[0][0])]
    assert handed, "the remaining steps were not marked handed_off"
    sql = str(handed[0][0][0])
    assert "status IN ('pending', 'presented')" in sql, sql
    assert "node_key" not in sql, "all_remaining must not scope to one node"


@pytest.mark.asyncio
async def test_a_blocked_handoff_starts_nothing():
    db = _db(SESSION, [HANDS_ON])
    with patch("app.modules.supervised_runs.channel", new=AsyncMock(return_value=None)), \
         patch("app.modules.execution_agent._classify_dag_executability",
               new=AsyncMock(return_value={"total": 1, "nonexec": 1, "hands_on": True, "by_text": 1})), \
         patch("app.modules.run_broker.start") as start:
        res = await ah.start_all_remaining(db, "s1")
    assert res["started"] is False and res["blocker"]
    start.assert_not_called()
    assert not [c for c in db.execute.await_args_list if "handed_off" in str(c[0][0])], \
        "steps were handed off although the run could not start"
