"""§17.2067 — routine events log at INFO; the level follows the outcome (§17.2061, continued).

Live (2026-11-28, laptop engine, job 0fb4ea7f RD83/RDV83 -- two clean redeploys): every WARNING the engine logged that
morning was routine. `node_edit_reopened_terminal_job` (adding the redeploy step to the completed job reopens it -- what
the edit means, every redeploy), `supervised_run_already_met_blind ... why=the step's work is an act` (§17.2059b's gate
declining exactly as designed), and `execute_all_nodes_cleanup ... (reason=None)` (§17.2066). The same flows also log a
step found already met (`supervised_run_already_met`, `..._after_approval`) and a redraft that came back clean
(`*_redraft_clean`, `rehearsal_repair_clean`) at WARNING: normal outcomes.

These drive the PRODUCTION code: `node_editor.insert_node` (a fake session), `supervised_runs.already_met` (the real
blindness rules; the runner read and the judge faked), and `resolve_run`'s after-approval read (§17.2059's harness).
The other side of each split is pinned too: checks that cannot witness the step still WARN.
"""
from __future__ import annotations

import ast
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import node_editor as ne
from app.modules import supervised_runs as sr

pytestmark = pytest.mark.smoke

JID = "00000000-0000-0000-0000-0000000020a7"


def _records(caplog, prefix):
    return [(r.levelno, r.getMessage()) for r in caplog.records if r.getMessage().startswith(prefix + " ")]


def _warnings(caplog):
    return [r.getMessage()[:200] for r in caplog.records if r.levelno >= logging.WARNING]


# ── node_edit_reopened_terminal_job ──────────────────────────────────────────

class _EditSession:
    """A completed two-step job; the reopen UPDATE moves it when it is terminal."""

    def __init__(self, status):
        self.status, self.sql = status, []

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        self.sql.append(sql)
        outer = self
        moved = None
        if sql.startswith("UPDATE jobs SET status = 'executing'") and self.status in ("completed", "failed", "cancelled"):
            self.status, moved = "executing", ("id",)

        class R:
            def fetchone(self_inner):
                return moved

            def mappings(self_inner):
                class M:
                    def all(self2):
                        return [{"node_key": k, "status": "done", "depends_on": d, "execution_order": i,
                                 "edit_version": 1, "is_deliverable": False}
                                for i, (k, d) in enumerate([("T1", []), ("RD82", ["T1"])])]
                return M()
        return R()

    async def commit(self):
        self.sql.append("COMMIT")


@pytest.mark.asyncio
async def test_adding_a_step_to_a_finished_job_reopens_it_at_info(caplog):
    db = _EditSession("completed")
    with caplog.at_level(logging.DEBUG):
        out = await ne.insert_node(JID, {"node_key": "RD83", "title": "VM 110: redeploy the engine at main",
                                         "depends_on": ["RD82"]}, db=db)
    assert out == {"status": "ok", "node_key": "RD83"}
    assert db.status == "executing"
    assert _records(caplog, "node_edit_reopened_terminal_job") == [
        (logging.INFO, f"node_edit_reopened_terminal_job job={JID} -> executing")]
    assert _warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_job_still_open_is_not_reopened_and_logs_nothing(caplog):
    db = _EditSession("blocked")
    with caplog.at_level(logging.DEBUG):
        await ne.insert_node(JID, {"node_key": "RD83", "title": "x", "depends_on": ["RD82"]}, db=db)
    assert db.status == "blocked" and _records(caplog, "node_edit_reopened_terminal_job") == []


# ── supervised_run_already_met_blind: split by reason ────────────────────────

async def _confirm_everything(claim, cmds, pasted, expects=None):
    return [{"id": f"V{i}", "verdict": "confirmed", "reason": "shown", "claim": claim} for i, _ in enumerate(cmds, 1)]


def _node(title, done_when="`curl -s http://localhost:8000/health` prints `\"status\":\"healthy\"`"):
    return {"node_key": "RD83", "title": title, "description": "", "prompt_template": f"{title}. Done when {done_when}."}


async def _already_met(caplog, node, frame):
    probes = AsyncMock(side_effect=AssertionError("a blind pre-read reads nothing"))
    with patch("app.modules.assist_local_runner.run_probes", probes), \
         patch.object(sr, "_verify_verdicts", _confirm_everything), caplog.at_level(logging.DEBUG):
        met = await sr.already_met(SimpleNamespace(name="lab-runner"), node, frame, {}, policy={"allow": ["ANY"]})
    probes.assert_not_awaited()
    assert met is None
    return _records(caplog, "supervised_run_already_met_blind")


@pytest.mark.asyncio
@pytest.mark.parametrize("node, frame, why", [
    # the live RD83: "redeploy" in the title (§17.2059b)
    (_node("VM 110: redeploy the engine at main fbb05cdf"),
     {"commands": ["cd /opt/scaffold-engine && git pull"], "verify": ["curl -s http://localhost:8000/health"]},
     "the step's work is an act (\"redeploy\" in its title)"),
    # an act in the block, not the title
    (_node("Apply the panel's new unit file", "`systemctl is-active panel` prints `active`"),
     {"commands": ["systemctl daemon-reload", "systemctl restart panel"], "verify": ["systemctl is-active panel"]},
     "the block's work is an act (`systemctl restart"),
    # a backup is taken now (§17.1655)
    (_node("Backup Prowlarr config database", "`ls /srv/backups/prowlarr` lists a file"),
     {"commands": ["tar czf /srv/backups/prowlarr/db.tgz /srv/prowlarr"], "verify": ["ls /srv/backups/prowlarr"]},
     "backup"),
])
async def test_a_step_never_met_by_its_kind_is_the_gate_working_at_info(caplog, node, frame, why):
    logs = await _already_met(caplog, node, frame)
    assert len(logs) == 1 and logs[0][0] == logging.INFO, logs
    assert why in logs[0][1], logs
    assert _warnings(caplog) == []


@pytest.mark.asyncio
async def test_checks_that_cannot_witness_the_step_still_warn(caplog):
    """§17.1976 — the drafted checks read only the PLACE (the VM is up): a defect in the draft, not the gate's design."""
    node = _node("VM 110: install jq", "`qm status 110` prints `status: running`")
    frame = {"commands": ["qm guest exec 110 -- apt-get install -y jq"], "verify": ["qm status 110"]}
    assert sr.never_met_by_design(node, frame) == ""
    logs = await _already_met(caplog, node, frame)
    assert len(logs) == 1 and logs[0][0] == logging.WARNING, logs
    assert "act" not in logs[0][1] and sr.already_met_blind(node, frame, ["qm status 110"]) in logs[0][1]


# ── supervised_run_already_met_after_approval ────────────────────────────────

@pytest.mark.asyncio
async def test_a_step_found_already_met_after_the_approval_is_a_normal_outcome_at_info(caplog):
    from tests import test_a_check_after_the_approval_finds_a_step_already_met_17_2059 as h
    waiting = h._waiting(h._frame())
    with caplog.at_level(logging.DEBUG):
        out, _sql, _calls = await h._resolve(waiting, health=h.MET)
    assert out["outcome"] == "already_met" and out["node_status"] == "done", out
    logs = _records(caplog, "supervised_run_already_met_after_approval")
    assert len(logs) == 1 and logs[0][0] == logging.INFO, logs
    assert "node=MF9 checks=2" in logs[0][1]
    assert _warnings(caplog) == []


# ── the inline levels inside `_pause_for_decision` ───────────────────────────
#
# `supervised_run_already_met` and the eight redraft/repair `_clean` lines live inline in `_pause_for_decision`, a
# 1,000-line function no unit harness drives end to end. They are pinned here from the parsed module (the CALL, not a
# substring): the named event must be logged by `logger.info`, and its sibling that reports a refusal must stay a
# warning -- so this gate bites in both directions.

EA = Path(__file__).resolve().parents[1] / "app" / "modules" / "execution_agent.py"
ROUTINE = ("supervised_run_already_met", "supervised_run_redraft_clean", "supervised_run_redraft_again_clean",
           "supervised_run_redraft_last_clean", "rehearsal_repair_clean", "supervised_run_no_commands_redraft_clean",
           "supervised_run_verify_redraft_clean", "supervised_run_all_reads_redraft_clean",
           "runbook_coverage_redraft_clean")
STILL_WARN = ("supervised_run_redraft", "supervised_run_redraft_rejected", "supervised_run_redraft_again_rejected",
              "supervised_run_redraft_last_rejected", "already_met_check_failed")


def _levels_by_event() -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for n in ast.walk(ast.parse(EA.read_text())):
        if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "logger" and n.args and isinstance(n.args[0], ast.Constant)
                and isinstance(n.args[0].value, str)):
            out.setdefault(n.args[0].value.split(" ")[0].rstrip(":"), []).append(n.func.attr)
    return out


@pytest.mark.parametrize("event", ROUTINE)
def test_a_routine_outcome_in_the_pause_logs_at_info(event):
    assert _levels_by_event().get(event) == ["info"], (event, _levels_by_event().get(event))


@pytest.mark.parametrize("event", STILL_WARN)
def test_a_refusal_or_failure_in_the_pause_still_warns(event):
    assert _levels_by_event().get(event) == ["warning"], (event, _levels_by_event().get(event))
