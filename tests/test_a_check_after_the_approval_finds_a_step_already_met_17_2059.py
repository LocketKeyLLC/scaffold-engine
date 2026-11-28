"""§17.2059 — a step run inside a guest learns it was already done: its checks run after the approval, before the block.

§17.2051 (right) stopped sending the pre-approval "already met?" read when it needs what only the approval supplies --
every step run inside a guest over ssh has `GUEST_USER="<AI_USER>" bash /tmp/in_vm_110.sh check '…' # $MASS_PASSWORD`.
That left those steps never learning they were already done. A step whose work is an ACT -- a redeploy (RD82), a
restart -- is the exception (§17.2059b, below): its checks read the same before and after it, so it always runs.

These drive the PRODUCTION `check_after_approval` (where the frame is drawn) and `resolve_run` (the approved run) down
through the real `run_verify` -> `run_block` / `write_files_on` -> `mint_approval`; only the runner's MCP call, the
channel, the secret store and the judge's model are faked. The fake runner answers from what it is sent; the fake judge
READS its evidence: it confirms a section only when it shows the setting the step makes true.
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
CONF = "/opt/scaffold-engine/config/models.conf"
CHECK = ("GUEST_USER=\"<AI_USER>\" bash /tmp/in_vm_110.sh check "
         f"'grep MODEL_FALLBACK {CONF}' # $MASS_PASSWORD")
SCRIPT = ("#!/usr/bin/env bash\nUSER_NAME=\"${GUEST_USER}\"\n"
          "if [ \"${1:-}\" = check ]; then ssh \"$USER_NAME@10.0.0.5\" \"$2\"; exit $?; fi\n"
          "cat > /tmp/in_vm_110_remote.sh <<'REMOTE'\nset -e\n"
          f"sed -i 's/^MODEL_FALLBACK=.*/MODEL_FALLBACK=deepseek-v4-pro:cloud/' {CONF}\nREMOTE\n")
# a STATE the step makes true (§17.2059b: a redeploy/restart is an act and is never already met -- see below)
NODE = {"node_key": "MF9", "title": "VM 110: set the engine's fallback model to deepseek-v4-pro:cloud",
        "description": "", "prompt_template": (f"Inside VM 110 set MODEL_FALLBACK=deepseek-v4-pro:cloud in {CONF}. "
                                               f"Done when `grep MODEL_FALLBACK {CONF}` prints "
                                               "`MODEL_FALLBACK=deepseek-v4-pro:cloud`.")}
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
        ok = "MODEL_FALLBACK=deepseek-v4-pro:cloud" in body
        out.append({"id": p["id"], "verdict": "confirmed" if ok else "unknown",
                    "reason": "the setting shows it" if ok else "nothing shows it",
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


MET = "MODEL_FALLBACK=deepseek-v4-pro:cloud"
NOT_MET = "MODEL_FALLBACK=qwen3:4b"


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
    assert "## Already met — nothing was run" in done["out"] and MET in done["out"]
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
             {"path": CONF, "content": MET + "\n"}]
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
    node_no_own = {**NODE, "prompt_template": "Inside VM 110 set the fallback model."}
    frame = _frame(verify=(place,))
    assert sr.check_after_approval(node_no_own, frame, POLICY) == ""
    forged = {**_waiting(frame, flagged=False), "description": node_no_own["prompt_template"],
              "prompt_template": node_no_own["prompt_template"], "check_after_approval": "forged"}
    out, _sql, calls = await _resolve(forged, health="status: running " + MET)
    kinds = _kinds(calls)
    assert out.get("already_met") is None and kinds[:2] == ["write /tmp/in_vm_110.sh", "phase"], kinds   # block first


def test_the_rule_says_which_frames_wait_for_the_approval():
    # hits: a check that needs an <INPUT> or an engine-held $NAME
    assert "<AI_USER>" in sr.check_after_approval(NODE, _frame(), POLICY)
    assert "$MASS_PASSWORD" in sr.check_after_approval(
        NODE, _frame(verify=(f"bash /tmp/in_vm_110.sh check 'grep MODEL_FALLBACK {CONF}' # $MASS_PASSWORD",)),
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


# ---------------------------------------------------------------------------------------------------------------
# §17.2059b — a step whose work is an ACT (redeploy, rebuild, restart) is never already met: its checks read the same
# state before the act and after it. Live: job 0fb4ea7f RD82, template deploy_scaffold_engine; its Done when is true
# before every redeploy, so the after-approval read would have confirmed it and the merged commit never deployed.
# ---------------------------------------------------------------------------------------------------------------
RD82 = {"node_key": "RD82", "tool": "Shell", "node_type": "task",
        "title": "VM 110: redeploy the engine at main 92c26e33", "description": "",
        "prompt_template": (
            "Inside VM 110: redeploy the scaffold engine's services at the newest commit of its ref (main 92c26e33; it "
            "carries §17.2055 (PR #1234: a literal the step gives is never drafted back as a placeholder)).\n"
            "MODEL_ROLE_LEARNING_ENABLED=false\n"
            "Done when `curl -s http://localhost:8000/health` prints `\"status\":\"healthy\"` and `docker inspect -f "
            "'{{index .Config.Labels \"com.docker.compose.project.config_files\"}}' ollama` prints "
            "`docker-compose.nogpu.yml`.")}
TRUTH_110 = SimpleNamespace(kind="vm", gid="110", agent=False, name="ai-vm")


def _template_frame(node, remote=None):
    """The frame the production template path draws for ``node`` (select → values → render → parse)."""
    from app.modules import runbook_templates as rt
    t = rt.select_template(node, TRUTH_110)
    vals = rt.values_for(t, node, TRUTH_110, {}, model_values=({"REMOTE_COMMANDS": remote} if remote else None))
    rb = rt.render(t, vals)
    return t.name, {"commands": sr.runbook_commands(rb), "verify": sr.verify_commands(rb), "files": sr.file_writes(rb),
                    "runbook": rb}


async def _healthy(_spec, cmds, _env):
    out = "".join(f"== V{i} ==\n" + ('{"status":"healthy"} 1a2b3c4d' if "health" in c else
                                     "docker-compose.yml,docker-compose.nogpu.yml" if "config_files" in c else
                                     "status: running") + "\n" for i, c in enumerate(cmds, 1))
    return out, [{"id": f"V{i}", "command": c, "ok": True, "ran": True} for i, c in enumerate(cmds, 1)]


async def _confirm_everything(claim, cmds, pasted, expects=None):
    return [{"id": f"V{i}", "verdict": "confirmed", "reason": "shown", "claim": claim} for i, _ in enumerate(cmds, 1)]


@pytest.mark.asyncio
async def test_the_rd82_redeploy_is_never_already_met_even_on_a_healthy_engine():
    name, frame = _template_frame(RD82)
    assert name == "deploy_scaffold_engine"
    verify = sr.with_own_checks(RD82, frame["verify"], frame["commands"])
    assert len(verify) == 3                                    # qm status + both Done-when checks, inside the guest
    assert "act" in sr.already_met_blind(RD82, frame, verify)
    assert sr.check_after_approval(RD82, frame, POLICY) == ""   # the frame is not flagged ...
    waiting = {**frame, "node_key": "RD82", "title": RD82["title"], "description": RD82["prompt_template"],
               "prompt_template": RD82["prompt_template"], "check_after_approval": "forged"}
    with patch.object(sr, "run_verify", _healthy), patch.object(sr, "_verify_verdicts", _confirm_everything):
        met, wrote = await sr.already_met_after_approval(
            SimpleNamespace(name="lab-runner"), waiting, "RD82", verify, frame["files"], {"MASS_PASSWORD": PW})
    assert met is None and wrote == []                         # ... and a forged flag still reads nothing


def _act_node(key, title):
    return {"node_key": key, "title": title, "description": "",
            "prompt_template": f"{title}. Done when `curl -s http://localhost:8000/health` prints `\"status\":\"healthy\"`."}


@pytest.mark.asyncio
@pytest.mark.parametrize("title, commands, remote", [
    # the act in the block, not the title (host side)
    ("Bring the engine to main", ["cd /opt/scaffold-engine && git pull && docker compose up -d --build"], None),
    ("Apply the panel's new unit file", ["systemctl daemon-reload", "systemctl restart panel"], None),
    # the act inside the guest: in the template's REMOTE body
    ("Inside VM 110: apply the Ollama settings", None, "systemctl restart ollama"),
])
async def test_an_act_in_the_block_is_never_already_met(title, commands, remote):
    node = _act_node("ACT1", title)
    if remote:
        _name, frame = _template_frame(node, remote)
    else:
        frame = {"commands": commands, "verify": ["curl -s http://localhost:8000/health"], "files": []}
    verify = sr.with_own_checks(node, frame["verify"], frame["commands"])
    assert "act" in sr.already_met_blind(node, frame, verify)
    assert sr.check_after_approval(node, frame, POLICY) == ""
    probes = AsyncMock(side_effect=AssertionError("nothing is read"))
    with patch("app.modules.assist_local_runner.run_probes", probes), \
         patch.object(sr, "_verify_verdicts", _confirm_everything):
        assert await sr.already_met(SimpleNamespace(name="r"), node, frame, {}, policy=POLICY) is None
    probes.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_state_change_is_still_found_already_met():
    """§17.1302's own case: installing a package is a state, and `dpkg -s` reads it."""
    node = {"node_key": "JQ1", "title": "Install jq on the host", "description": "",
            "prompt_template": "Install jq. Done when `dpkg -s jq` prints `install ok installed`."}
    frame = {"commands": ["apt-get install -y jq"], "verify": ["dpkg -s jq"], "files": []}
    assert sr.the_work_is_an_act(node, frame) == ""
    probes = AsyncMock(return_value=("== V1 ==\nStatus: install ok installed\n",
                                     [{"id": "V1", "command": "dpkg -s jq", "ok": True, "ran": True}]))
    with patch("app.modules.assist_local_runner.run_probes", probes), \
         patch.object(sr, "_verify_verdicts", _confirm_everything):
        assert await sr.already_met(SimpleNamespace(name="r"), node, frame, {}, policy=POLICY)


def test_the_guest_wrapper_is_not_the_work():
    """The ssh template's own script restarts nothing; an install inside the guest is no act, and is still flagged."""
    node = {"node_key": "JQ2", "title": "VM 110: install jq", "description": "",
            "prompt_template": "Inside VM 110: install jq. Done when `dpkg -s jq` prints `install ok installed`."}
    _name, frame = _template_frame(node, "apt-get install -y jq")
    assert sr.the_work_is_an_act(node, frame) == ""
    assert sr.check_after_approval(node, frame, POLICY)
