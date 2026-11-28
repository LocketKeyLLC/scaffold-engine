"""§17.2060 — "Done when it prints the `uptime -s` line" is checked deterministically, not by the judge model alone.

§17.2050 judges a dropped run's earlier output deterministically only when its Done when names an EXACT print
(§17.1630: a backtick right after the verb). The RDV read-back steps (job 0fb4ea7f RDV80) say "Done when it prints the
`uptime -s` line": nothing deterministic read that, so a judge that saw a timestamp anywhere in the output -- another
command's, or none from `uptime -s` at all -- decided alone. Now that command must have run in the block, exited 0 and
printed something that is not an error, as a REQUIRED condition beside the judge. A read-only in-guest block prints a
heading and an exit per statement (`runbook_templates.with_sections`), so its one log can be told apart.

The resolve_run cases drive the PRODUCTION `resolve_run` -> `met_before_the_drop` with only the runner, channel and
judge's model faked. The fake judge is the one a too-generous model would be: it confirms ANY section that holds a
timestamp, whichever command printed it -- exactly what this rule must not let through.
"""
from __future__ import annotations

import re
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_state_check as sc
from app.modules import operator_steps as ops
from app.modules import runbook_templates as rt
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
_TS = re.compile(r"\b\d{4}-\d\d-\d\d[ T]\d\d:\d\d:\d\d")


async def _generous_judge(probes, pasted, **_kw):
    sections = sc.attribute_sections(pasted)
    return [{"id": p["id"], "verdict": "confirmed" if _TS.search(str(sections.get(p["id"]) or "")) else "unknown",
             "reason": "a boot time is printed", "claim": p["claim"], "command": p["command"]} for p in probes]


def _guest(*sections):
    """A wait phase's output: the in-guest log, one `$ cmd` / lines / `-- exit N` section per statement."""
    return "\n".join(f"$ {c}\n{body}\n-- exit {rc}" if body else f"$ {c}\n-- exit {rc}" for c, body, rc in sections)


def _executed(before):
    return [
        {"command": PHASE.format("start"), "ok": True, "exit": 0, "output": "started scaffold-step inside 910"},
        {"command": PHASE.format("wait"), "ok": True, "exit": 0, "output": before},
        {"command": PHASE.format("wait"), "ok": False, "exit": None, "output": DROP, "unreachable": True},
    ]


async def _resolve(waiting, executed):
    db = AsyncMock()
    claim = MagicMock()
    claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim)
    db.commit = AsyncMock()
    ran = [{"id": f"V{i}", "command": c, "ran": False, "why": "no answer"} for i, c in enumerate(waiting["verify"], 1)]
    with patch.object(sr, "channel", AsyncMock(return_value=(SimpleNamespace(name="lab-runner"), {"allow": ["ANY"]}))), \
         patch("app.modules.assist_supervised.gate_block", return_value=(list(waiting["commands"]), [])), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=executed)), \
         patch("app.modules.assist_local_runner.run_probes", new=AsyncMock(return_value=("", ran))), \
         patch("app.modules.rewrite_guard.destructive_rewrites", new=AsyncMock(return_value=[])), \
         patch("app.modules.address_watch.around_run", new=AsyncMock(return_value=([], {}))), \
         patch("app.modules.address_watch.after_run", new=AsyncMock(return_value=None)), \
         patch("app.modules.machine_truth.after_step_done", new=AsyncMock(return_value=None)), \
         patch("app.modules.assist_state_check.judge_outputs", new=_generous_judge), \
         patch.object(sr, "_RECHECK_DELAY_S", 0), \
         patch.object(sr, "diagnose_failure", AsyncMock(return_value="")):
        return await sr.resolve_run(db, "job-2060", waiting["node_key"], "run", waiting)


DEPLOYED = ("cat /opt/scaffold-engine/DEPLOYED", "deployed 1a2b3c4d fix(x): something (#1)", 0)
STARTED = ("docker inspect -f '{{.State.StartedAt}}' scaffold-orchestrator", "2026-11-20T09:15:31.123Z", 0)


@pytest.mark.asyncio
@pytest.mark.parametrize("before", [
    _guest(DEPLOYED, ("uptime -s", "2026-11-20 09:14:07", 0), STARTED),                 # the live RDV shape
    _guest(DEPLOYED, ("uptime -s | tee /tmp/boot", "2026-11-20 09:14:07", 0)),           # one stage of a pipeline
])
async def test_the_named_command_ran_and_printed_its_line(before):
    out = await _resolve(READ_BACK, _executed(before))
    assert out["node_status"] == "done" and out["met_before_drop"] is True, out.get("reason")


@pytest.mark.asyncio
@pytest.mark.parametrize("before", [
    _guest(DEPLOYED, ("uptime -s", "", 1), STARTED),                                     # it failed; another printed a time
    _guest(DEPLOYED, STARTED),                                                           # it never ran
    _guest(DEPLOYED, ("uptime -s", "uptime: cannot read /proc/uptime", 0), STARTED),     # it printed an error
    _guest(DEPLOYED, ("uptime", " 09:20:01 up 6 min,  1 user,  load average: 0.10", 0), STARTED),   # a DIFFERENT command
    _guest(DEPLOYED, ("echo uptime -s", "uptime -s", 0), STARTED),                       # its name, not its output
    "deployed 1a2b3c4d\n2026-11-20 09:14:07\nscaffold-orchestrator started",            # no sections: cannot be told apart
])
async def test_another_commands_output_never_stands_in_for_it(before):
    out = await _resolve(READ_BACK, _executed(before))
    assert out["node_status"] == "failed" and out["unknown_outcome"] is True, out


@pytest.mark.asyncio
@pytest.mark.parametrize("rc, status", [(0, "done"), (1, "failed")])
async def test_a_host_side_read_is_its_own_section(rc, status):
    """Not inside a guest: each command the runner ran is its own record -- `uptime -s` exited ``rc``."""
    w = {**READ_BACK, "commands": ["cat /opt/x/DEPLOYED", "uptime -s", "systemctl is-active orchestrator"],
         "verify": ["uptime -s"]}
    executed = [
        {"command": "cat /opt/x/DEPLOYED", "ok": True, "exit": 0, "output": "deployed 1a2b3c4d at 2026-11-20 09:00:00"},
        {"command": "uptime -s", "ok": rc == 0, "exit": rc, "informational": rc != 0,
         "output": "2026-11-20 09:14:07" if rc == 0 else ""},
        {"command": "systemctl is-active orchestrator", "ok": False, "exit": None, "output": DROP, "unreachable": True},
    ]
    out = await _resolve(w, executed)
    assert out["node_status"] == status, out.get("reason")


def test_the_done_when_wording_is_read():
    def named(t):
        return ops.named_outputs({"prompt_template": t})
    # hits
    assert named("Read it. Done when it prints the `uptime -s` line.") == ["uptime -s"]
    assert named("Done when it shows the `df -h /` output and the `free -m` lines.") == ["df -h /", "free -m"]
    assert named("Done when it prints the output of `systemctl is-active ollama`.") == ["systemctl is-active ollama"]
    # misses: an exact print is §17.1630's, a check line is not a print, and text outside the Done when is not read
    assert named("Done when it prints `deploy ok r42`.") == []
    assert named("Done when `systemctl is-active ollama` prints active.") == []
    assert named("Print the `uptime -s` line. Done when the file exists.") == []


def test_a_command_is_matched_whole():
    assert sr._runs_named("uptime -s", "uptime -s")
    assert sr._runs_named("uptime -s | tee /tmp/boot", "uptime -s")
    assert sr._runs_named("bash /tmp/in_vm_9.sh check 'uptime -s'", "uptime -s")
    assert not sr._runs_named("uptime", "uptime -s")
    assert not sr._runs_named("echo uptime -s", "uptime -s")
    assert not sr._runs_named("uptime -s -p", "uptime -s")


def test_a_read_only_guest_block_prints_a_section_per_statement_bash_level():
    """The rendered body, run by bash under §17.1608's read-only prologue: every statement is a section with its exit."""
    body = ("cat /etc/hostname\nfor u in alpha beta; do\n  echo unit $u\ndone\n"
            "ls /nonexistent-2060\necho boot 2026-11-20 09:14:07")
    assert rt.strict_line(body).startswith("set +e")
    rendered = rt.read_only_remote(body)
    run = subprocess.run(["bash", "-c", rt.strict_line(body) + "\n" + rendered], capture_output=True, text=True)
    assert run.returncode == 0, run.stderr
    secs = sr.output_sections([{"command": "bash /tmp/in_vm_9.sh last", "ok": True, "exit": 0, "output": run.stdout}])
    by = {s["command"]: s for s in secs}
    assert by["cat /etc/hostname"]["exit"] == 0 and by["cat /etc/hostname"]["lines"]
    assert by["for u in alpha beta; do echo unit $u; done"]["lines"] == ["unit alpha", "unit beta"]
    assert by["ls /nonexistent-2060"]["exit"] != 0 and by["ls /nonexistent-2060"]["lines"] == []
    assert sr.named_output_unmet(["cat /etc/hostname", "echo boot 2026-11-20 09:14:07"],
                                 [{"command": "x", "ok": True, "exit": 0, "output": run.stdout}]) == []
    assert sr.named_output_unmet(["ls /nonexistent-2060"],
                                 [{"command": "x", "ok": True, "exit": 0, "output": run.stdout}])
    # the record's "Ran inside the guest" lists the step's commands, not the template's headings
    script = "cat > /tmp/x <<'REMOTE'\n" + rendered + "\nREMOTE\n"
    head = sr._frame_runbook({"files": [{"path": "/tmp/in_vm_9.sh", "content": script}]}, ["bash /tmp/in_vm_9.sh start"], [])
    assert "printf" not in head and "cat /etc/hostname" in head


def test_a_body_the_split_cannot_follow_and_a_write_block_are_unchanged():
    heredoc = "python3 - <<'PY'\nprint(1)\nPY"
    assert rt.with_sections(heredoc) == heredoc
    write = "systemctl restart ollama\nsystemctl is-active ollama"
    assert rt.read_only_remote(write) == write
