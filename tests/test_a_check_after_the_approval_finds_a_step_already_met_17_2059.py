"""§17.2059 — a step run inside a guest learns it was already done: its checks run after the approval, before the block.

§17.2051 (right) stopped sending the pre-approval "already met?" read when it needs what only the approval supplies --
every step run inside a guest over ssh has `GUEST_USER="<AI_USER>" bash /tmp/in_vm_110.sh check '…' # $MASS_PASSWORD`.
That left those steps never learning they were already done: approving RD80 (job 0fb4ea7f, every redeploy) rebuilt an
engine that was already on the merged commit.

These drive the PRODUCTION `check_after_approval` (where the frame is drawn) and `resolve_run` (the approved run) down
through the real `run_verify` -> `run_block` / `write_files_on` -> `mint_approval`; only the runner's MCP call, the
channel, the secret store and the judge's model are faked. The fake runner answers from what it is sent; the fake judge
READS its evidence: it confirms a section only when it shows `"status": "ok"` AND the merged commit.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_state_check as sc
from app.modules import assist_supervised as sw
from app.modules import supervised_runs as sr

TOKEN = "runner-token-2059"
PW = "pw-2059-not-real"
PHASE = 'MASS_PASSWORD="$MASS_PASSWORD" GUEST_USER="<AI_USER>" bash /tmp/in_vm_110.sh {}'
CHECK = ("GUEST_USER=\"<AI_USER>\" bash /tmp/in_vm_110.sh check "
         "'curl -s http://localhost:8000/health' # $MASS_PASSWORD")
SCRIPT = ("#!/usr/bin/env bash\nUSER_NAME=\"${GUEST_USER}\"\n"
          "if [ \"${1:-}\" = check ]; then ssh \"$USER_NAME@10.0.0.5\" \"$2\"; exit $?; fi\n"
          "cat > /tmp/in_vm_110_remote.sh <<'REMOTE'\nset -e\ncd /opt/scaffold-engine && make redeploy\nREMOTE\n")
NODE = {"node_key": "RD9", "title": "VM 110: redeploy the engine at the merged commit 1a2b3c4d",
        "description": "", "prompt_template": ("Pull the merged commit 1a2b3c4d and rebuild the orchestrator inside "
                                               "VM 110. Done when `curl -s http://localhost:8000/health` answers "
                                               "status ok on commit 1a2b3c4d.")}
POLICY = {"allow": ["ANY"], "secrets": [], "held": ["MASS_PASSWORD"]}


def _frame(verify=(CHECK,), files=None, commands=None):
    return {"kind": "run", "node_key": NODE["node_key"], "runbook": "## Run this", "refused": [],
            "commands": list(commands or [PHASE.format(p) for p in ("start", "wait", "last")]),
            "verify": list(verify), "inputs": [{"name": "AI_USER", "hint": "the guest account"}],
            "files": list(files if files is not None else [{"path": "/tmp/in_vm_110.sh", "content": SCRIPT}])}


def _waiting(frame, *, flagged=True):
    """The frame as `park_awaiting_decision` stores it; the flag is what the PRODUCTION rule says at park time."""
    w = {**frame, "title": NODE["title"], "description": NODE["prompt_template"],
         "prompt_template": NODE["prompt_template"]}
    if flagged:
        why = sr.check_after_approval(NODE, frame, POLICY)
        if why:
            w["check_after_approval"] = why
    return w


async def _reading_judge(probes, pasted, **_kw):
    sections = sc.attribute_sections(pasted)
    out = []
    for p in probes:
        body = str(sections.get(p["id"]) or "")
        ok = '"status": "ok"' in body and "1a2b3c4d" in body
        out.append({"id": p["id"], "verdict": "confirmed" if ok else "unknown",
                    "reason": "the health answer shows it" if ok else "nothing shows it",
                    "claim": p["claim"], "command": p["command"]})
    return out


async def _resolve(waiting, *, health):
    """Run the operator's approval of ``waiting``; the guest answers its health check with ``health``."""
    calls: list[dict] = []

    async def call_tool(spec, tool, payload):
        calls.append({"tool": tool, **payload})
        if tool == sw.WRITE_FILE_TOOL:
            return SimpleNamespace(text="wrote", structured=None)
        cmd = payload["command"]
        if " check " in cmd:
            return SimpleNamespace(text=f"[exit 0]\n{health}", structured=None)
        return SimpleNamespace(text="[exit 0]\nscaffold-RD9 finished (exit 0)", structured=None)

    db = AsyncMock()
    claim = MagicMock()
    claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim)
    db.commit = AsyncMock()
    spec = SimpleNamespace(name="lab-runner", headers={"X-Runner-Token": TOKEN})
    with patch.object(sr, "channel", AsyncMock(return_value=(spec, {"allow": ["ANY"]}))), \
         patch("app.modules.mcp_client.call_tool", new=call_tool), \
         patch("app.modules.runner_secrets.values_for", new=AsyncMock(return_value={"MASS_PASSWORD": PW})), \
         patch("app.modules.rewrite_guard.destructive_rewrites", new=AsyncMock(return_value=[])), \
         patch("app.modules.address_watch.around_run", new=AsyncMock(return_value=([], {}))), \
         patch("app.modules.address_watch.after_run", new=AsyncMock(return_value=None)), \
         patch("app.modules.machine_truth.after_step_done", new=AsyncMock(return_value=None)), \
         patch("app.modules.assist_state_check.judge_outputs", new=_reading_judge), \
         patch.object(sr, "diagnose_failure", AsyncMock(return_value="")):
        out = await sr.resolve_run(db, "job-2059", waiting["node_key"], "run", waiting, inputs={"AI_USER": "labuser"})
    sql = [(str(c.args[0]), c.args[1] if len(c.args) > 1 else {}) for c in db.execute.await_args_list]
    return out, sql, calls


def _status_written(sql):
    return [m.group(1) for s, _p in sql for m in [re.search(r"UPDATE dag_nodes SET status = '(\w+)'", s)] if m]


def _kinds(calls):
    return ["write " + c["path"] if c["tool"] == sw.WRITE_FILE_TOOL
            else ("phase" if re.search(r"in_vm_110\.sh (?:start|wait|last)\b", c["command"]) else "check")
            for c in calls]


MET = '{"status": "ok", "commit": "1a2b3c4d"}'
NOT_MET = '{"status": "ok", "commit": "0ede435d"}'


@pytest.mark.asyncio
async def test_a_guest_step_already_done_is_found_after_the_approval_and_nothing_runs(caplog):
    waiting = _waiting(_frame())
    assert "<AI_USER>" in waiting["check_after_approval"]
    with caplog.at_level(logging.INFO, logger="scaffold"):
        out, sql, calls = await _resolve(waiting, health=MET)
    assert out["outcome"] == "already_met" and out["node_status"] == "done" and out["already_met"] is True, out
    # the frame's check and the step's own Done-when check, carried into the guest (§17.1625) -- and no phase ran
    assert _kinds(calls) == ["write /tmp/in_vm_110.sh", "check", "check"], _kinds(calls)
    assert _status_written(sql)[-1] == "done"
    done = [p for s, p in sql if "status = 'done'" in s][0]
    assert "after the approval" in done["why"] and "none of the block's commands ran" in done["why"]
    assert "## Already met — nothing was run" in done["out"] and "1a2b3c4d" in done["out"]
    assert PW not in done["out"] and PW not in str(out)
    # the check ran with the operator's answer bound and the engine-held value OUT OF BAND, on its own approval
    chk = [c for c in calls if c["tool"] == sw.WRITE_TOOL][0]
    assert 'GUEST_USER="labuser"' in chk["command"] and "<AI_USER>" not in chk["command"]
    assert chk["env"] == {"MASS_PASSWORD": PW} and PW not in chk["command"]
    assert any("supervised_run_already_met_after_approval" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_a_guest_step_not_yet_done_runs_after_its_checks():
    out, sql, calls = await _resolve(_waiting(_frame()), health=NOT_MET)
    kinds = _kinds(calls)
    assert kinds[:3] == ["write /tmp/in_vm_110.sh", "check", "check"], kinds     # the reads came first
    assert kinds.count("phase") == 3 and out.get("already_met") is None          # ... then the whole block ran
    assert out["outcome"] in ("ran", "failed")


@pytest.mark.asyncio
async def test_every_runner_call_carries_its_own_single_use_signed_approval():
    _out, _sql, calls = await _resolve(_waiting(_frame()), health=NOT_MET)
    ids = [c["approval"]["id"] for c in calls]
    assert len(ids) == len(set(ids)) == len(calls) >= 5
    key = sw.approval_key(TOKEN)
    for c in calls:
        signed = c["command"] if c["tool"] == sw.WRITE_TOOL else sw.write_file_message(c["path"], c["content"])
        a = c["approval"]
        want = hmac.new(key, sw.approval_message(a["id"], a["nonce"], a["exp"], signed), hashlib.sha256).hexdigest()
        assert a["sig"] == want, c


@pytest.mark.asyncio
async def test_only_the_engines_check_script_is_written_before_the_check():
    """The step's own file is its WORK: never written before the read that asks whether the work is done."""
    files = [{"path": "/tmp/in_vm_110.sh", "content": SCRIPT},
             {"path": "/etc/scaffold/redeploy.env", "content": "COMMIT=1a2b3c4d\n"}]
    out, _sql, calls = await _resolve(_waiting(_frame(files=files)), health=MET)
    assert out["outcome"] == "already_met"
    assert _kinds(calls) == ["write /tmp/in_vm_110.sh", "check", "check"], _kinds(calls)


@pytest.mark.asyncio
async def test_a_frame_parked_without_the_flag_runs_as_before():
    out, _sql, calls = await _resolve(_waiting(_frame(), flagged=False), health=MET)
    kinds = _kinds(calls)
    assert out["outcome"] == "ran" and "phase" in kinds[:2], kinds               # the block ran first, as on main


@pytest.mark.asyncio
async def test_a_place_only_check_never_proves_it_even_after_the_approval():
    """§17.1976 holds after the approval too: `qm status 110` says the VM is up, not that the step happened."""
    place = "qm status 110 # $MASS_PASSWORD"
    node_no_own = {**NODE, "prompt_template": "Pull the merged commit and rebuild inside VM 110."}
    frame = _frame(verify=(place,))
    assert sr.check_after_approval(node_no_own, frame, POLICY) == ""
    forged = {**_waiting(frame, flagged=False), "description": node_no_own["prompt_template"],
              "prompt_template": node_no_own["prompt_template"], "check_after_approval": "forged"}
    out, _sql, calls = await _resolve(forged, health="status: running 1a2b3c4d \"status\": \"ok\"")
    kinds = _kinds(calls)
    assert out.get("already_met") is None and kinds[:2] == ["write /tmp/in_vm_110.sh", "phase"], kinds   # block first


def test_the_rule_says_which_frames_wait_for_the_approval():
    # hits: a check that needs an <INPUT> or an engine-held $NAME
    assert "<AI_USER>" in sr.check_after_approval(NODE, _frame(), POLICY)
    assert "$MASS_PASSWORD" in sr.check_after_approval(
        NODE, _frame(verify=("bash /tmp/in_vm_110.sh check 'curl -s http://localhost:8000/health' # $MASS_PASSWORD",)),
        POLICY)
    # misses: a check readable now (the pre-read already asked it), a frame with no commands, a blind check
    plain = {**NODE, "prompt_template": "Restart the service. Done when `systemctl is-active arrsvc` prints active."}
    assert sr.check_after_approval(plain, {"commands": ["systemctl restart arrsvc"],
                                           "verify": ["systemctl is-active arrsvc"]}, POLICY) == ""
    assert sr.check_after_approval(NODE, {**_frame(), "commands": []}, POLICY) == ""
    assert sr.check_after_approval(NODE, {**_frame(), "always_run": "the block proves it"}, POLICY) == ""


def test_the_executor_flags_the_frame_where_it_is_drawn():
    import inspect

    from app.modules import execution_agent as ea
    src = inspect.getsource(ea)
    assert 'frame["check_after_approval"] = _after' in src
    assert "supervised_runs.check_after_approval(run_node, frame, policy)" in src
