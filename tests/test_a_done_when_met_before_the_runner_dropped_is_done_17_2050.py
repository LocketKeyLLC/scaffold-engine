"""§17.2050 — a step whose Done when was already met is not failed when the runner drops afterwards.

Live (2026-11-26, job 0fb4ea7f, the server-redeploy job): RDV80, a read-only read-back whose prompt ends "Done when it
prints the `uptime -s` line.", ran as in-guest phases through the runner. Its output had already printed
`deployed 0ede435d ...`, the `uptime -s` line and the orchestrator-started line; then the Proxmox host crashed, the next
`wait` call lost its connection (`the connection to pve-runner dropped while ... wait ... was running`), the verify
re-check came back blank (`verify_recheck_still_blank`), and the step was failed for the transport -- the job left
`blocked` on a step whose own finish condition sat in its output.

These drive the PRODUCTION `resolve_run` with only the runner (`run_block`, `run_probes`), the channel and the judge's
model faked. The fake judge READS the evidence it is handed: it confirms only a section that shows a boot timestamp or
the named deploy line, so a run that printed nothing useful is `unknown`, never confirmed by construction.
"""
from __future__ import annotations

import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_state_check as sc
from app.modules import supervised_runs as sr

DROP = ("(runner error: mcp server 'lab-runner' tool 'run_supervised': MCPError: SSE stream ended without a "
        "response)")
PHASE = "bash /tmp/in_vm_910.sh {}"
READ_BACK = {
    "kind": "run", "node_key": "RDV9", "runbook": "## Run this",
    "title": "VM 910: read back the deployed commit and boot time (read-only)",
    "description": "",
    "prompt_template": ("Read-only: inside VM 910 print the deployed commit, `uptime -s` and whether the orchestrator "
                        "started. Change nothing. Done when it prints the `uptime -s` line."),
    "commands": [PHASE.format(p) for p in ("start", "wait", "wait", "last")],
    "verify": [PHASE.format("check 'uptime -s'")], "refused": []}
# §17.2060 — a read-only in-guest block prints each statement's heading and exit (`runbook_templates.with_sections`)
BEFORE_DROP_GOOD = ("$ cat /opt/scaffold-engine/DEPLOYED\ndeployed 1a2b3c4d fix(x): something (#1)\n-- exit 0\n"
                    "$ uptime -s\n2026-11-20 09:14:07\n-- exit 0\n"
                    "$ docker logs scaffold-orchestrator 2>&1 | grep -m1 started\nscaffold-orchestrator started\n-- exit 0")
_EVIDENCE = re.compile(r"\b\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\b|deploy ok r42")


async def _reading_judge(probes, pasted, **_kw):
    """A judge that reads: `confirmed` only when the section shows the evidence the claims ask for."""
    sections = sc.attribute_sections(pasted)
    out = []
    for p in probes:
        body = str(sections.get(p["id"]) or "")
        out.append({"id": p["id"], "verdict": "confirmed" if _EVIDENCE.search(body) else "unknown",
                    "reason": "the section shows it" if _EVIDENCE.search(body) else "nothing shows it",
                    "claim": p["claim"], "command": p["command"]})
    return out


def _executed(before: str, *, first_ok=True):
    return [
        {"command": PHASE.format("start"), "ok": True, "exit": 0, "output": "started scaffold-step inside 910"},
        {"command": PHASE.format("wait"), "ok": first_ok, "exit": 0 if first_ok else 1, "output": before},
        {"command": PHASE.format("wait"), "ok": False, "exit": None, "output": DROP, "unreachable": True},
    ]


async def _resolve(waiting, executed, probes_answer=("", None)):
    db = AsyncMock()
    claim = MagicMock()
    claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim)
    db.commit = AsyncMock()
    pasted, ran = probes_answer
    ran = ran if ran is not None else [{"id": f"V{i}", "command": c, "ran": False, "why": "no answer"}
                                       for i, c in enumerate(waiting["verify"], 1)]
    with patch.object(sr, "channel", AsyncMock(return_value=(SimpleNamespace(name="lab-runner"), {"allow": ["ANY"]}))), \
         patch("app.modules.assist_supervised.gate_block", return_value=(list(waiting["commands"]), [])), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=executed)), \
         patch("app.modules.assist_local_runner.run_probes", new=AsyncMock(return_value=(pasted, ran))), \
         patch("app.modules.rewrite_guard.destructive_rewrites", new=AsyncMock(return_value=[])), \
         patch("app.modules.address_watch.around_run", new=AsyncMock(return_value=([], {}))), \
         patch("app.modules.address_watch.after_run", new=AsyncMock(return_value=None)), \
         patch("app.modules.machine_truth.after_step_done", new=AsyncMock(return_value=None)), \
         patch("app.modules.assist_state_check.judge_outputs", new=_reading_judge), \
         patch.object(sr, "_RECHECK_DELAY_S", 0), \
         patch.object(sr, "diagnose_failure", AsyncMock(return_value="")):
        out = await sr.resolve_run(db, "job-2050", waiting["node_key"], "run", waiting)
    sql = [(str(c.args[0]), c.args[1] if len(c.args) > 1 else {}) for c in db.execute.await_args_list]
    return out, sql


def _status_written(sql):
    return [m.group(1) for s, _p in sql for m in [re.search(r"UPDATE dag_nodes SET status = '(\w+)'", s)] if m]


@pytest.mark.asyncio
async def test_rdv80_the_done_when_was_in_the_output_before_the_drop(caplog):
    with caplog.at_level(logging.INFO, logger="scaffold"):
        out, sql = await _resolve(READ_BACK, _executed(BEFORE_DROP_GOOD))
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "verify_recheck_still_blank" in msgs and "supervised_run_met_before_drop" in msgs, msgs[-600:]
    assert out["outcome"] == "ran" and out["node_status"] == "done", out.get("reason")
    assert out["met_before_drop"] is True and out["confirmed_after_drop"] is False
    assert _status_written(sql)[-1] == "done"
    done = [p for s, p in sql if "status = 'done'" in s][0]
    assert "after the run's own output met the step's Done when" in done["why"]
    assert "The connection dropped after the step's Done when was met" in done["out"]


@pytest.mark.asyncio
@pytest.mark.parametrize("before", [
    "STILL RUNNING: scaffold-step (12s)",                         # template chatter only: nothing read yet
    "deployed 1a2b3c4d fix(x): something (#1)\nuptime: command not found",   # no boot line: the Done when is not met
])
async def test_output_that_does_not_meet_the_done_when_still_fails(before):
    out, sql = await _resolve(READ_BACK, _executed(before))
    assert out["outcome"] == "failed" and out["unknown_outcome"] is True
    assert _status_written(sql)[-1] == "failed"
    assert "dropped while" in out["reason"]


@pytest.mark.asyncio
async def test_a_change_step_whose_output_looks_right_is_not_done_on_it():
    """A write's output is not its evidence (§17.1691): with no named print, the drop leaves it unknown."""
    w = {**READ_BACK, "node_key": "RD9", "title": "VM 910: redeploy the engine at the merged commit",
         "prompt_template": "Pull the merged commit and rebuild inside VM 910. Done when the orchestrator answers."}
    out, sql = await _resolve(w, _executed(BEFORE_DROP_GOOD))
    assert out["outcome"] == "failed" and _status_written(sql)[-1] == "failed"


@pytest.mark.asyncio
@pytest.mark.parametrize("before, status", [
    ("pulled 1a2b3c4d\ndeploy ok r42", "done"),        # the named print is there and the judge confirms it
    ("pulled 1a2b3c4d\nbuilding image", "failed"),     # the named print never came
])
async def test_a_change_step_whose_done_when_names_its_print(before, status):
    w = {**READ_BACK, "node_key": "RD10", "title": "VM 910: redeploy the engine at the merged commit",
         "prompt_template": "Pull the merged commit and rebuild inside VM 910. Done when it prints `deploy ok r42`."}
    out, sql = await _resolve(w, _executed(before))
    assert out["node_status"] == status and _status_written(sql)[-1] == status, out.get("reason")


@pytest.mark.asyncio
async def test_checks_that_answered_after_the_drop_are_not_overridden():
    """The checks were read back and did not confirm: that is fresh evidence, the earlier output does not outvote it."""
    answered = ("== V1 ==\nuptime: cannot read /proc/uptime\n",
                [{"id": "V1", "command": READ_BACK["verify"][0], "ok": True, "ran": True}])
    out, sql = await _resolve(READ_BACK, _executed(BEFORE_DROP_GOOD), probes_answer=answered)
    assert out["outcome"] == "failed" and _status_written(sql)[-1] == "failed"
    assert "did NOT show its goal" in out["reason"]


@pytest.mark.asyncio
async def test_a_real_failure_before_the_drop_is_never_judged_on_output():
    out, sql = await _resolve(READ_BACK, _executed(BEFORE_DROP_GOOD, first_ok=False))
    assert out["outcome"] == "failed" and _status_written(sql)[-1] == "failed"
