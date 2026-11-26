r"""§17.2039 (#110) — a hands-on step with no runner is the operator's, not a runbook recorded done.

§17.624 parks a plan that is MOSTLY hands-on. Below that majority, a step that changes a machine took the ordinary
path whenever no runner was registered: its runbook was written as the output, the step was recorded `done`, and the
job finished `completed` (only the deliverable's banner said otherwise). §17.1183 already sends an LLM-tagged step
that does host work down that same path, so a mis-tagged step went the same way. Now `_hand_back_for_approval` hands
such a step back and marks it, and the drained loop parks it as `kind: "yours"`: the step in its own words, no
promised check (there is no machine to read), done only on the operator's word.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import execution_agent as ea
from app.modules import operator_steps as ops
from app.modules import supervised_runs as sr

SHELL = {"id": "00000000-0000-0000-0000-000000000001", "node_key": "T3", "title": "Open port 51413 on the firewall",
         "tool": "Shell", "node_type": "task", "depends_on": [], "execution_order": 3,
         "description": "Run `pct exec 105 -- iptables -A INPUT -p tcp --dport 51413 -j ACCEPT`. "
                        "Done when `pct exec 105 -- iptables -S` lists the rule."}
LLM = {**SHELL, "node_key": "T1", "title": "Summarise the options", "tool": "LLM", "execution_order": 1,
       "description": "Compare the two VPN providers in a short table."}


def _db(rows=(), metadata=None, brief=""):
    """A session that answers the three reads by their SQL and records every statement."""
    db = AsyncMock()
    db.sql = []

    async def execute(stmt, params=None):
        sql = str(stmt)
        db.sql.append((sql, dict(params or {})))
        r = MagicMock()
        if "SELECT metadata FROM jobs" in sql:
            r.scalar = MagicMock(return_value=json.dumps(metadata or {}))
        elif "SELECT refined_brief" in sql:
            r.scalar = MagicMock(return_value=brief)
        elif "FROM dag_nodes n" in sql:
            r.mappings = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[dict(x) for x in rows])))
        else:
            r.scalar = MagicMock(return_value={})
            r.rowcount = 1
        return r
    db.execute = execute
    db.commit = AsyncMock()
    return db


@pytest.fixture
def no_runner(monkeypatch):
    monkeypatch.setattr(sr, "runner_registered", AsyncMock(return_value=("none", "")))
    monkeypatch.setattr(sr.settings, "shell_tool_enabled", False)
    monkeypatch.setattr(sr.settings, "execution_supervised_runs_enabled", True)


@pytest.mark.asyncio
async def test_the_executor_hands_the_step_back_and_marks_it(no_runner):
    db = _db()
    out = await ea._hand_back_for_approval(db, "j", SHELL, "Shell")
    assert out and out["status"] == "needs_approval" and out["node_key"] == "T3"
    sql = " ".join(s for s, _p in db.sql)
    assert "operators_without_runner" in sql
    assert "SET status = 'pending'" in sql


@pytest.mark.asyncio
async def test_the_drained_loop_finds_it_as_the_operators(no_runner):
    db = _db(rows=[LLM, SHELL])
    node = await ops.pending_operator_step(db, "j")
    assert node is not None and node["node_key"] == "T3" and node["no_runner"] is True


@pytest.mark.asyncio
async def test_a_marked_step_is_found_though_it_does_not_read_hands_on(no_runner):
    """The executor may have rerouted an LLM step (a deploy job's verify, §17.1545) that `is_hands_on_node` misses."""
    db = _db(rows=[LLM], metadata={ops.WITHOUT_RUNNER: ["T1"]})
    node = await ops.pending_operator_step(db, "j")
    assert node is not None and node["node_key"] == "T1" and node["no_runner"] is True


@pytest.mark.asyncio
async def test_a_registered_runner_keeps_the_old_hold(monkeypatch):
    """The correct miss: with a runner, the step is the runner's (§17.1186), never handed to the operator."""
    monkeypatch.setattr(sr, "runner_registered", AsyncMock(return_value=("registered", "pve-runner")))
    db = _db(rows=[LLM, SHELL])
    assert await ops.pending_operator_step(db, "j") is None


@pytest.mark.asyncio
async def test_the_question_is_the_step_itself_with_no_promised_check(no_runner, monkeypatch):
    monkeypatch.setattr(ops, "_walkthrough", AsyncMock(side_effect=AssertionError("no model draw")))
    frame = await ops.frame_operator({**SHELL, "no_runner": True})
    assert frame["kind"] == "yours" and frame["verify"] == []
    assert "iptables -A INPUT" in frame["walkthrough"] and "No runner is registered" in frame["walkthrough"]
    assert frame["options"][0]["label"] == "I've done it"


@pytest.mark.asyncio
async def test_done_is_recorded_on_the_operators_word_with_their_note(no_runner):
    db = _db()
    frame = await ops.frame_operator({**SHELL, "no_runner": True})
    res = await ops.resolve_operator(db, "j", "T3", "done", frame, note="rule is listed")
    assert res["outcome"] == "done"
    upd = [p for s, p in db.sql if "SET status = 'done'" in s][0]
    assert "rule is listed" in upd["out"]
