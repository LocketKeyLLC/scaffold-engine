"""§17.1186 — Auto mode's hands-on steps go through the supervised write
channel: drafted, gated, parked for approval, run on approval, verified."""
from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import supervised_runs as sr
from app.modules import execution_agent as ea
from app.modules import decision_pause as dp

RUNBOOK = """## Prerequisites
- Container 111 exists.

## Run this
1. Start it:
```bash
pct start 111
```
2. Set DNS:
```bash
pct set 111 --nameserver 192.168.1.1
```

## Verify
- `pct status 111` reports `status: running`.
- Resolution works: `pct exec 111 -- getent hosts example.org`.

## Rollback
```bash
pct stop 111
```
"""
POLICY = {"allow": ["pct start", "pct set"], "sudo": True, "helper": "11"}
NODE = {"node_key": "ADD50", "title": "Start container 111", "prompt_template": "Done when `pct status 111` reports running.",
        "depends_on": [], "tool": "LLM", "node_type": "task", "hands_on_reason": "observed:pct status 111"}


def _spec():
    s = MagicMock(); s.name = "pve-runner"; s.headers = {"X-Runner-Token": "tok"}; return s


# ── the runbook's commands ───────────────────────────────────────────────

def test_run_commands_come_from_run_this_and_verify_from_verify_only():
    assert sr.runbook_commands(RUNBOOK) == ["pct start 111", "pct set 111 --nameserver 192.168.1.1"]
    assert sr.verify_commands(RUNBOOK) == ["pct status 111", "pct exec 111 -- getent hosts example.org"]


def test_a_runbook_without_sections_uses_every_fence():
    assert sr.runbook_commands("```\npct start 111\n```\ntext\n```\npct status 111\n```") == ["pct start 111", "pct status 111"]
    assert sr.verify_commands("no verify section here `pct status 1`") == []


def test_frame_offers_run_only_when_the_gate_is_clean():
    f = sr.frame_run(NODE, RUNBOOK, _spec(), POLICY)
    assert f["kind"] == "run" and [o["id"] for o in f["options"]] == ["run", "myself", "skip"] and f["suggested"] == "run"
    assert f["commands"] == ["pct start 111", "pct set 111 --nameserver 192.168.1.1"] and f["refused"] == []
    assert f["verify"] == ["pct status 111", "pct exec 111 -- getent hosts example.org"] and f["runner"] == "pve-runner"
    f2 = sr.frame_run(NODE, RUNBOOK, _spec(), {"allow": ["pct start"], "sudo": False})
    assert [o["id"] for o in f2["options"]] == ["myself", "skip"] and f2["suggested"] == "myself"
    assert f2["refused"][0]["command"].startswith("pct set") and "cannot run it as written" in f2["question"]


# ── what is waiting ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pending_hands_on_skips_decided_and_decision_nodes_and_human_steps():
    db = AsyncMock()
    r1 = MagicMock(); r1.scalar.return_value = {"ADD50": {"by": "operator", "choice": "myself"}}
    r2 = MagicMock(); r2.mappings.return_value.all.return_value = [
        {"node_key": "ADD50", "title": "Start container 111", "prompt_template": "Done when `pct status 111` reports running.",
         "depends_on": [], "tool": "LLM", "node_type": "task", "description": None, "retry_count": 0, "last_verification_reason": None},
        {"node_key": "T2", "title": "Decide", "prompt_template": "Done when `pct status 1` shows x.", "depends_on": [], "tool": "LLM",
         "node_type": "decision", "description": None, "retry_count": 0, "last_verification_reason": None},
        {"node_key": "H1", "title": "Ask", "prompt_template": "", "depends_on": [], "tool": "human", "node_type": "task",
         "description": None, "retry_count": 0, "last_verification_reason": None},
        {"node_key": "T9", "title": "Write docs", "prompt_template": "Write it.", "depends_on": [], "tool": "LLM", "node_type": "task",
         "description": None, "retry_count": 0, "last_verification_reason": None},
        {"node_key": "ADD88", "title": "Install Caddy", "prompt_template": "Write the file via `tee -a`.", "depends_on": [], "tool": "LLM",
         "node_type": "task", "description": None, "retry_count": 0, "last_verification_reason": None},
    ]
    db.execute = AsyncMock(side_effect=[r1, r2])
    with patch.object(settings, "shell_tool_enabled", False), patch.object(settings, "mcp_tool_enabled", True):
        node = await sr.pending_hands_on(db, "j")
    assert node["node_key"] == "ADD88" and node["hands_on_reason"] == "writes:tee -a"
    sql = db.execute.await_args_list[1].args[0].text
    assert "status = 'pending'" in sql and "NOT EXISTS" in sql and "ORDER BY n.execution_order" in sql


@pytest.mark.asyncio
async def test_channel_is_none_when_off_or_without_a_runner_or_policy(monkeypatch):
    monkeypatch.setattr(settings, "execution_supervised_runs_enabled", False)
    assert await sr.channel(AsyncMock()) is None
    monkeypatch.setattr(settings, "execution_supervised_runs_enabled", True)
    monkeypatch.setattr(settings, "mcp_tool_enabled", True)
    with patch("app.modules.assist_local_runner.runner_spec", new=AsyncMock(return_value=None)):
        assert await sr.channel(AsyncMock()) is None
    with patch("app.modules.assist_local_runner.runner_spec", new=AsyncMock(return_value=_spec())), \
         patch("app.modules.assist_supervised.write_policy", new=AsyncMock(return_value=None)):
        assert await sr.channel(AsyncMock()) is None
    with patch("app.modules.assist_local_runner.runner_spec", new=AsyncMock(return_value=_spec())), \
         patch("app.modules.assist_supervised.write_policy", new=AsyncMock(return_value=POLICY)):
        spec, pol = await sr.channel(AsyncMock())
    assert spec.name == "pve-runner" and pol == POLICY


# ── resolution ───────────────────────────────────────────────────────────

def _db(rowcounts):
    db = AsyncMock()
    results = []
    for rc in rowcounts:
        r = MagicMock(); r.rowcount = rc; results.append(r)
    db.execute = AsyncMock(side_effect=results)
    return db


@pytest.mark.asyncio
async def test_skip_and_myself_write_the_node_without_touching_the_runner():
    waiting = {"kind": "run", "runbook": RUNBOOK, "commands": ["pct start 111"], "verify": [], "refused": []}
    db = _db([1])
    out = await sr.resolve_run(db, "j", "ADD50", "skip", waiting)
    assert out["outcome"] == "skipped" and "status = 'skipped'" in db.execute.await_args.args[0].text
    db = _db([1])
    out = await sr.resolve_run(db, "j", "ADD50", "myself", waiting)
    assert out["outcome"] == "runbook" and db.execute.await_args.args[1]["out"] == RUNBOOK
    assert (await sr.resolve_run(_db([]), "j", "ADD50", "explode", waiting))["outcome"] == "bad_choice"


@pytest.mark.asyncio
async def test_run_claims_runs_verifies_and_marks_done_with_the_report(monkeypatch):
    waiting = {"kind": "run", "runbook": RUNBOOK, "commands": ["pct start 111", "pct set 111 --nameserver 192.168.1.1"],
               "verify": ["pct status 111"], "refused": []}
    db = _db([1, 1])
    executed = [{"command": "pct start 111", "output": "", "exit": 0, "ok": True, "approval_id": "a", "refused": False},
                {"command": "pct set 111 --nameserver 192.168.1.1", "output": "", "exit": 0, "ok": True, "approval_id": "b", "refused": False}]
    with patch.object(sr, "channel", new=AsyncMock(return_value=(_spec(), POLICY))), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=executed)) as rb, \
         patch("app.modules.assist_local_runner.run_probes", new=AsyncMock(return_value=("== V1 ==\nstatus: running\n", [{"id": "V1", "command": "pct status 111", "ok": True, "chars": 15}]))):
        out = await sr.resolve_run(db, "j", "ADD50", "run", waiting)
    assert out["outcome"] == "ran" and out["node_status"] == "done"
    assert rb.await_args.args[1] == ["pct start 111", "pct set 111 --nameserver 192.168.1.1"]
    claim_sql = db.execute.await_args_list[0].args[0].text
    assert "SET status = 'running'" in claim_sql and "status = 'pending'" in claim_sql
    done_sql, done_params = db.execute.await_args_list[1].args
    assert "SET status = 'done'" in done_sql.text and "status = 'running'" in done_sql.text
    assert "## Executed on pve-runner" in done_params["out"] and "$ pct start 111" in done_params["out"]
    assert "## Verify results\n$ pct status 111\nstatus: running" in done_params["out"]


@pytest.mark.asyncio
async def test_a_failed_command_fails_the_node_with_the_reason():
    waiting = {"kind": "run", "runbook": RUNBOOK, "commands": ["pct start 111", "pct set 111 --nameserver 192.168.1.1"], "verify": ["pct status 111"], "refused": []}
    db = _db([1, 1])
    executed = [{"command": "pct start 111", "output": "CT 111 does not exist", "exit": 2, "ok": False, "approval_id": "a", "refused": False}]
    with patch.object(sr, "channel", new=AsyncMock(return_value=(_spec(), POLICY))), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=executed)), \
         patch("app.modules.assist_local_runner.run_probes", new=AsyncMock()) as probes:
        out = await sr.resolve_run(db, "j", "ADD50", "run", waiting)
    assert out["outcome"] == "failed" and "exited 2" in out["reason"]
    probes.assert_not_awaited()                                   # no verify after a failure
    sql, params = db.execute.await_args_list[1].args
    assert "SET status = 'failed'" in sql.text and params["why"].startswith("supervised run stopped")


@pytest.mark.asyncio
async def test_run_is_refused_when_the_frame_carried_refusals_or_the_channel_closed():
    waiting = {"kind": "run", "runbook": RUNBOOK, "commands": ["pct destroy 111"], "verify": [], "refused": [{"command": "pct destroy 111", "why": "x"}]}
    assert (await sr.resolve_run(_db([]), "j", "ADD50", "run", waiting))["outcome"] == "not_runnable"
    waiting["refused"] = []
    with patch.object(sr, "channel", new=AsyncMock(return_value=None)):
        assert (await sr.resolve_run(_db([]), "j", "ADD50", "run", waiting))["outcome"] == "no_channel"
    with patch.object(sr, "channel", new=AsyncMock(return_value=(_spec(), POLICY))):
        out = await sr.resolve_run(_db([]), "j", "ADD50", "run", waiting)   # pct destroy is not on the list any more
    assert out["outcome"] == "not_runnable" and out["refused"][0]["command"] == "pct destroy 111"


@pytest.mark.asyncio
async def test_resolve_decision_dispatches_a_run_pause_and_records_the_result():
    waiting = {"kind": "run", "node_key": "ADD50", "runbook": RUNBOOK, "commands": ["pct start 111"], "verify": [], "refused": []}
    db = AsyncMock()
    row = MagicMock(); row.mappings.return_value.first.return_value = {"status": "awaiting_decision", "metadata": {"awaiting_decision": waiting}}
    meta = MagicMock(); meta.rowcount = 1
    db.execute = AsyncMock(side_effect=[row, meta])
    with patch("app.modules.supervised_runs.resolve_run", new=AsyncMock(return_value={"outcome": "ran", "node_status": "done"})) as rr, \
         patch("app.modules.decision_pause.transition", new=AsyncMock(return_value=True)) as tr:
        out = await dp.resolve_decision(db, "j", "ADD50", choice="run", note="go")
    assert rr.await_args.args[3] == "run" and out["outcome"] == "ran" and out["record"]["node_status"] == "done"
    assert json.loads(db.execute.await_args_list[1].args[1]["patch"])["decisions"]["ADD50"]["result"] == "ran"
    assert tr.await_args.kwargs["to"] == "executing"


# ── the executor is wired ────────────────────────────────────────────────

def test_executor_wiring():
    pause = inspect.getsource(ea._pause_for_decision)
    assert "supervised_runs.channel(db)" in pause and "pending_hands_on(db, job_id)" in pause and "frame_run(" in pause
    assert "draft_runbook(" in pause and 'logger.warning("supervised_run_parked' in pause
    serial = inspect.getsource(ea.execute_all_nodes)
    assert "_hands_on_peek(node)" in serial and 'status in ("stale", "needs_approval")' in serial
    par = inspect.getsource(ea._run_parallel_frontier)
    assert "exclude=held" in par and 'status == "needs_approval"' in par and "held.add(" in par
    claim = inspect.getsource(ea._claim_ready_nodes_sql)
    assert "AND NOT (n.node_key = ANY(:exclude))" in claim
    seam = inspect.getsource(ea.execute_next_node)
    assert "_hand_back_for_approval(db, job_id, node, tool)" in seam
    assert seam.index("hands_on_step_as_runbook") < seam.index("_hand_back_for_approval(") < seam.index("NotImplementedError(")
    hb = inspect.getsource(ea._hand_back_for_approval)
    assert '"status": "needs_approval"' in hb and "SET status = 'pending', started_at = NULL" in hb and "status = 'running'" in hb
    gate = inspect.getsource(ea._classify_dag_executability)
    assert "supervised_runs.channel(db)" in gate and 'not why.startswith("tool:human")' in gate


@pytest.mark.asyncio
async def test_gate_counts_hands_on_steps_as_executable_when_the_channel_is_open(monkeypatch):
    monkeypatch.setattr(settings, "shell_tool_enabled", False)
    monkeypatch.setattr(settings, "hands_on_assist_gate_threshold", 0.5)
    db = AsyncMock(); res = MagicMock()
    res.mappings.return_value.all.return_value = [
        {"tool": "Shell", "node_type": "task", "node_key": "T1", "title": "x", "prompt_template": ""},
        {"tool": "LLM", "node_type": "task", "node_key": "T2", "title": "Start", "prompt_template": "Done when `pct status 111` reports running."},
        {"tool": "human", "node_type": "task", "node_key": "T3", "title": "ask", "prompt_template": ""},
    ]
    db.execute = AsyncMock(return_value=res)
    with patch("app.modules.supervised_runs.channel", new=AsyncMock(return_value=(_spec(), POLICY))):
        cls = await ea._classify_dag_executability(db, "j")
    assert cls["nonexec"] == 1 and cls["hands_on"] is False          # only the human step
    with patch("app.modules.supervised_runs.channel", new=AsyncMock(return_value=None)):
        cls = await ea._classify_dag_executability(db, "j")
    assert cls["nonexec"] == 3 and cls["hands_on"] is True
