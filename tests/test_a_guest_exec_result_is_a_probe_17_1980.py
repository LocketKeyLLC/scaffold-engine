"""§17.1980 — a probe that ran in a VM through `qm guest exec` is judged like any other probe.

Live shape (job 7cc2a7ac ADD6, 2026-11-21): GET/PUT/GET of the panel's settings answered 200, then
`qm guest exec <vm> -- cmp <settings> <its copy>` exited 0 on the host with `"exitcode" : 1` and `cmp: EOF on …` in
`err-data` -- the panel's save had dropped the file's last newline. The step stayed failed: nothing read the guest's
answer. Paths, addresses and ids below are made up.
"""
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import found_faults as tf
from app.modules import supervised_runs as sr

CFG = "/srv/game/Config/ServerSettings.ini"
BAK = CFG + ".bak-1a2b3c4d"
PANEL = "http://10.66.0.9:3001/api/game-settings"
AUTH = "--config /dev/stdin \"%s\" <<< \"user = \\\"panel:$PANEL_PASSWORD\\\"\"" % PANEL

ADD6 = {"title": "Proxmox host: exercise the game settings control and confirm the effect",
        "description": "GET the settings, PUT the same body back, GET again and compare."}


def _guest(cmd, code, out="", err="", exited=1):
    d = {"exitcode": code, "exited": exited}
    if out:
        d["out-data"] = out
    if err:
        d["err-data"] = err
    return {"command": f"qm guest exec 701 -- {cmd}", "output": json.dumps(d, indent=3).replace('":', '" :'),
            "exit": 0, "ok": True}


def _curl(label, path, code):
    return {"command": f"bash -c 'curl -sS -o {path} -w \"{label}: HTTP %{{http_code}}\\n\" {AUTH}'",
            "output": f"{label}: HTTP {code}", "exit": 0, "ok": True}


ADD6_RUN = [
    {"command": "write /tmp/cfg_cmp.py", "output": "wrote /tmp/cfg_cmp.py (612 bytes, 26 lines)", "exit": 0, "ok": True},
    _guest(f"cp -a {CFG} {BAK}", 0),
    _guest(f"cmp {CFG} {BAK}", 0),
    _curl("GET before", "/tmp/cfg-before.json", 200),
    _curl("PUT", "/tmp/cfg-put.out", 200),
    _curl("GET after", "/tmp/cfg-after.json", 200),
    {"command": "python3 /tmp/cfg_cmp.py", "output": "same", "exit": 0, "ok": True},
    _guest(f"cmp {CFG} {BAK}", 1, err=f"cmp: EOF on {CFG} after byte 3636, line 2\n"),
]
ADD6_REFUTED = [{"verdict": "contradicted",
                 "reason": f'cmp reports a difference: "cmp: EOF on {CFG} after byte 3636, line 2"'}]


def test_the_live_shape_is_done_with_its_finding():
    section, why = tf.findings_of_a_run(ADD6, ADD6_RUN, ADD6_REFUTED)
    assert why.startswith(tf.FINDINGS_MARK), why
    assert "found a difference" in why and "after byte 3636" in why
    assert "found 1 fault(s)" in why
    assert section.startswith("## Findings")


def test_the_guest_answer_is_read_as_the_guest_command():
    inner = tf._in_the_guest(ADD6_RUN[-1])
    assert inner["command"] == f"cmp {CFG} {BAK}" and inner["exit"] == 1 and "EOF on" in inner["output"]
    assert tf._probe(ADD6_RUN[-1])[1] and not tf._probe(ADD6_RUN[2])[1]       # 1 = a difference, 0 = a match
    # a curl inside the guest reads its written-out code just as it does on the host
    p, f = tf._probe(_guest(f"curl -s -o /dev/null -w '%{{http_code}}' {PANEL}", 0, out="503"))
    assert f and not p


def _without_fault(run):
    return run[:-1] + [_guest(f"cmp {CFG} {BAK}", 0)]


@pytest.mark.parametrize("run", [
    # every comparison matched: nothing found
    _without_fault(ADD6_RUN),
    # the guest agent is down: the test could not reach the thing at all
    ADD6_RUN[:-1] + [{"command": f"qm guest exec 701 -- cmp {CFG} {BAK}", "exit": 255, "ok": False,
                      "output": "QEMU guest agent is not running"}],
    # the guest's own tooling broke, inside out-data/err-data
    ADD6_RUN[:-1] + [_guest(f"cmp {CFG} {BAK}", 127, err="bash: cmp: command not found\n")],
    ADD6_RUN[:-1] + [_guest(f"cmp {CFG} {BAK}", 2, err=f"cmp: {BAK}: No such file or directory\n")],
    # a Node probe in the guest crashed: its stack sits on its own lines inside err-data, escaped in the host's JSON
    ADD6_RUN[:-1] + [_guest("node /opt/t/probe.mjs", 1, out="PASS overview\nFAIL settings\n",
                            err="Error: connect ECONNREFUSED 10.66.0.9:3001\n    at TCPConnectWrap.afterConnect "
                                "[as oncomplete] (node:net:1611:16)\n")],
    # still running when the agent answered: no result to judge
    ADD6_RUN[:-1] + [_guest(f"cmp {CFG} {BAK}", 0, exited=0)],
    # the difference is the step's OWN copy not landing, right after it made it
    ADD6_RUN[:2] + [_guest(f"cmp {CFG} {BAK}", 1, err=f"cmp: EOF on {BAK} after byte 12, line 1\n")] + ADD6_RUN[3:7],
])
def test_correct_misses(run):
    assert tf.findings_of_a_run(ADD6, run, ADD6_REFUTED) == ("", "")


def test_a_step_that_does_not_test_is_not_rescued():
    assert tf.findings_of_a_run({"title": "VM 701: write the game settings from the panel"},
                                ADD6_RUN, ADD6_REFUTED) == ("", "")


async def _resolve(node, run, verdicts):
    db = AsyncMock()
    claim = MagicMock(); claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim); db.commit = AsyncMock()
    cmds = [e["command"] for e in run]
    verify = [f"qm guest exec 701 -- cmp {CFG} {BAK}"]
    waiting = {"kind": "run", "node_key": "ADD6", **node, "prompt_template": node.get("description", ""),
               "runbook": "## Run this", "commands": cmds, "verify": verify, "refused": [], "files": []}
    with patch.object(sr, "channel", AsyncMock(return_value=(SimpleNamespace(name="pve-runner"), {"allow": ["ANY"]}))), \
         patch("app.modules.assist_supervised.gate_block", return_value=(cmds, [])), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=[dict(e) for e in run])), \
         patch("app.modules.rewrite_guard.destructive_rewrites", new=AsyncMock(return_value=[])), \
         patch("app.modules.address_watch.around_run", new=AsyncMock(return_value=([], {}))), \
         patch("app.modules.address_watch.after_run", new=AsyncMock(return_value=None)), \
         patch("app.modules.machine_truth.after_step_done", new=AsyncMock(return_value=None)), \
         patch.object(sr, "run_verify", AsyncMock(return_value=("", []))), \
         patch.object(sr, "_verify_verdicts", AsyncMock(return_value=verdicts)), \
         patch.object(sr, "diagnose_failure", AsyncMock(return_value="")):
        res = await sr.resolve_run(db, "j", "ADD6", "run", waiting)
    return res, db.execute.await_args_list[-1].args[1]


@pytest.mark.asyncio
async def test_resolve_run_records_the_guest_difference_as_a_finding():
    res, row = await _resolve(ADD6, ADD6_RUN, ADD6_REFUTED)
    assert res["node_status"] == "done", res
    assert row["why"].startswith(tf.FINDINGS_MARK) and "after byte 3636" in row["why"]
    res, row = await _resolve(ADD6, ADD6_RUN[:-1] + [_guest(f"cmp {CFG} {BAK}", 127, err="bash: cmp: command not found\n")],
                              ADD6_REFUTED)
    assert res["node_status"] == "failed", res
