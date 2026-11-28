"""§17.2061 — a routine supervised run logs no WARNING; a refused or failed one still does.

Live (2026-11-27, laptop engine, job 0fb4ea7f RD82/RDV82): one clean redeploy produced ~45 WARNING lines and no real
problem -- `supervised_run ... exit=0 refused=False` once per command (39), `supervised_run_started`,
`runbook_from_template`, `guest_evidence_carried`, `supervised_run_parked ... refused=0`, `supervised_run_done ...
confirmed_after_drop=False` -- burying the warnings that mean something. §17.2052 fixed one such case
(`job_transition_refused`); this pass levels the rest: routine at INFO, the problem case of the same event at WARNING.

These drive the PRODUCTION `run_block` (with only the runner's transport faked: each line really runs in bash),
`resolve_run` on top of it, and the park log line `_pause_for_decision` emits.
"""
from __future__ import annotations

import logging
import re
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_supervised as sw
from app.modules import execution_agent as ea
from app.modules import supervised_runs as sr

pytestmark = pytest.mark.smoke

CLEAN = ["true", "echo deployed 1a2b3c4d", "grep -q nothing-here /dev/null", "echo restarted"]


async def _bash(spec_, tool, args):
    """The runner's transport: the line really runs; a line naming `forbidden` is refused the way the runner says."""
    cmd = args["command"]
    r = MagicMock()
    r.structured = None
    if "forbidden" in cmd:
        r.text = "(refused by the local runner: not on the allow-list)"
        return r
    p = subprocess.run(["bash", "-c", cmd], capture_output=True, text=True, timeout=10)
    r.text = f"[exit {p.returncode}]\n{p.stdout}{p.stderr}"
    return r


def _spec():
    spec = MagicMock()
    spec.name = "pve-runner"
    spec.headers = {"X-Runner-Token": "tok"}
    return spec


def _records(caplog, prefix: str, level: int):
    return [r for r in caplog.records if r.levelno == level and r.getMessage().startswith(prefix + " ")]


def _warnings(caplog):
    return [r.getMessage()[:160] for r in caplog.records if r.levelno >= logging.WARNING]


@pytest.mark.asyncio
async def test_a_clean_block_logs_each_command_at_info_and_warns_nothing(caplog):
    with patch("app.modules.mcp_client.call_tool", new=_bash), caplog.at_level(logging.DEBUG, logger="scaffold"):
        done = await sw.run_block(_spec(), CLEAN)
    assert [d["command"] for d in done] == CLEAN
    assert done[2]["informational"] and done[2]["exit"] == 1, "the read's no is an answer, not a failure"
    assert len(_records(caplog, "supervised_run", logging.INFO)) == len(CLEAN)
    assert _warnings(caplog) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("block, bad", [
    (["true", "echo forbidden-write", "echo never"], "echo forbidden-write"),      # refused by the runner
    (["true", "echo ok", "touch /proc/nope-2061"], "touch /proc/nope-2061"),        # a write that failed
])
async def test_a_refused_or_failed_command_still_warns(caplog, block, bad):
    with patch("app.modules.mcp_client.call_tool", new=_bash), caplog.at_level(logging.DEBUG, logger="scaffold"):
        done = await sw.run_block(_spec(), block)
    assert done[-1]["command"] == bad and not done[-1]["ok"]
    warned = _records(caplog, "supervised_run", logging.WARNING)
    assert len(warned) == 1 and repr(bad[:120]) in warned[0].getMessage()
    assert len(_records(caplog, "supervised_run", logging.INFO)) == len(done) - 1, "the clean ones stay INFO"


WAITING = {
    "kind": "run", "node_key": "RD2061", "runbook": "## Run this",
    "title": "VM 910: redeploy the engine at the merged commit",
    "description": "", "prompt_template": "Pull and rebuild inside VM 910. Done when it prints `deployed`.",
    "commands": CLEAN, "verify": ["echo deployed"], "refused": []}


async def _resolve(waiting):
    db = AsyncMock()
    claim = MagicMock()
    claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim)
    db.commit = AsyncMock()
    ran = [{"id": "V1", "command": waiting["verify"][0], "ok": True, "ran": True, "exit": 0}]
    with patch.object(sr, "channel", AsyncMock(return_value=(_spec(), {"allow": ["ANY"]}))), \
         patch("app.modules.assist_supervised.gate_block", return_value=(list(waiting["commands"]), [])), \
         patch("app.modules.mcp_client.call_tool", new=_bash), \
         patch("app.modules.assist_local_runner.run_probes",
               new=AsyncMock(return_value=("== V1 ==\ndeployed\n", ran))), \
         patch("app.modules.rewrite_guard.destructive_rewrites", new=AsyncMock(return_value=[])), \
         patch("app.modules.address_watch.around_run", new=AsyncMock(return_value=([], {}))), \
         patch("app.modules.address_watch.after_run", new=AsyncMock(return_value=None)), \
         patch("app.modules.machine_truth.after_step_done", new=AsyncMock(return_value=None)), \
         patch("app.modules.assist_state_check.judge_outputs",
               new=AsyncMock(side_effect=lambda probes, pasted, **_k: [
                   {"id": p["id"], "verdict": "confirmed", "reason": "prints deployed", "claim": p["claim"],
                    "command": p["command"]} for p in probes])), \
         patch.object(sr, "_RECHECK_DELAY_S", 0), \
         patch.object(sr, "diagnose_failure", AsyncMock(return_value="")):
        return await sr.resolve_run(db, "job-2061", waiting["node_key"], "run", waiting)


@pytest.mark.asyncio
async def test_a_clean_supervised_run_through_resolve_run_warns_nothing(caplog):
    with caplog.at_level(logging.DEBUG, logger="scaffold"):
        out = await _resolve(WAITING)
    assert out["node_status"] == "done", out.get("reason")
    msgs = [r.getMessage() for r in caplog.records]
    assert any(m.startswith("supervised_run_started ") for m in msgs)
    assert any(m.startswith("supervised_run_done ") for m in msgs)
    assert _warnings(caplog) == []


@pytest.mark.asyncio
async def test_a_failed_supervised_run_through_resolve_run_still_warns(caplog):
    w = {**WAITING, "commands": ["true", "touch /proc/nope-2061", "echo never"]}
    with caplog.at_level(logging.DEBUG, logger="scaffold"):
        out = await _resolve(w)
    assert out["node_status"] != "done"
    warned = " ".join(_warnings(caplog))
    assert "supervised_run runner=" in warned and "nope-2061" in warned
    assert re.search(r"supervised_run_failed job=job-2061", warned), warned[-400:]


@pytest.mark.parametrize("refused, level", [
    ([], logging.INFO),
    ([{"command": "rm -rf /srv", "why": "not on the allow-list"}], logging.WARNING),
    ([{"command": "a", "why": "x"}, {"command": "b", "why": "y"}], logging.WARNING),
])
def test_parking_a_clean_draft_is_info_and_a_refused_one_warns(caplog, refused, level):
    frame = {"commands": ["true", "echo a"], "refused": refused, "runner": "pve-runner"}
    with caplog.at_level(logging.DEBUG):
        ea._log_parked("job-2061", {"node_key": "RD2061", "hands_on_reason": "tool:shell"}, frame)
    parked = [r for r in caplog.records if r.getMessage().startswith("supervised_run_parked ")]
    assert len(parked) == 1 and parked[0].levelno == level
    assert f"refused={len(refused)}" in parked[0].getMessage()
