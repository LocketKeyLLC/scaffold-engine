"""§17.1979 — a stop with nothing to stop does not end a ``set -e`` script; a real error still does.

Live (job 0fb4ea7f LM14, 2026-11-20): the draft carried ``pkill -f 'llama-server --model …'; sleep 5; docker exec
ollama ollama ps`` into the in-guest remote script, which opens with ``set -e``. ``pkill`` exits 1 when no process
matches (the runner already gone), which ends the script there, so the restart after it never runs. The engine now
guards such a stop so ONLY its documented "nothing to stop" exit is tolerated (``|| [ $? -eq 1 ]`` for pkill/killall,
an ``inspect`` that says "No such" for ``docker stop|rm``), and says so on the frame (``engine_fixed``).
``systemctl stop`` is left to fail loudly: its "not loaded" 5 is also a misspelled unit's.
"""
from __future__ import annotations

import os
import pathlib
import re
import shutil
import subprocess

import pytest

from app.modules import machine_truth as mt
from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

NODE = {"node_key": "LM14", "title": "VM 110: restart the benchmark A/B",
        "description": "Restart the benchmark inside VM 110."}
ENV = {"profile": "root@pve", "system_state": {"110": {"kind": "vm", "attrs": {"name": "bench-vm"}}}}
# the live shape, the names made up; the pattern can match no process on any machine the test runs on
LIVE = "pkill -f 'qq-bench-runner --model /srv/blobs/sha256-0f0f'; sleep 0; docker exec ollama ollama ps"
GUARDED = "pkill -f 'qq-bench-runner --model /srv/blobs/sha256-0f0f' || [ $? -eq 1 ]; sleep 0; docker exec ollama ollama ps"
AFTER = "systemd-run --unit=bench-new --collect bash /root/bench.sh"


def _render(remote: str) -> str:
    truth = mt.GuestTruth(gid="110", kind="vm", status="running", agent=False, key_known_by="ADD26")
    return rt.render(rt.REACH_VM_SSH_AND_RUN, rt.values_for(rt.REACH_VM_SSH_AND_RUN, NODE, truth, ENV,
                                                            {"REMOTE_COMMANDS": remote}))


def _frame(remote: str) -> dict:
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    policy = {"allow": ["ANY"], "sudo": True, "helper": "24", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
    return sr.frame_run(NODE, _render(remote), spec, policy, env=ENV)


def _remote_body(script: str) -> str:
    return re.search(r"<<'REMOTE'\n(.*?)\nREMOTE\n", script, re.S).group(1)


def _repaired(body: str) -> str:
    files, _ = sr.repair_no_op_stops_under_set_e([{"path": "/tmp/x.sh", "content": body}])
    return files[0]["content"]


def _run(body: str, tmp: pathlib.Path, stubs: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """The script, run for real by bash; `docker` and `systemd-run` (and any stub given) only log."""
    b = tmp / "bin"; b.mkdir(exist_ok=True)
    stubs = {"docker": f"echo \"docker $*\" >> {tmp}/log\n",
             "systemd-run": f"echo \"systemd-run $*\" >> {tmp}/log\n", **(stubs or {})}
    for name, text in stubs.items():
        (b / name).write_text("#!/bin/sh\n" + text)
        (b / name).chmod(0o755)
    p = tmp / "s.sh"; p.write_text(body + "\n")
    assert subprocess.run(["bash", "-n", str(p)]).returncode == 0
    return subprocess.run(["bash", str(p)], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "PATH": f"{b}:{os.environ['PATH']}"})


def _log(tmp: pathlib.Path) -> str:
    return (tmp / "log").read_text() if (tmp / "log").exists() else ""


#: the REAL pkill (procps) where the machine has one; an image without procps gets pkill's documented exits in its
#: place (1 = no process matched, 2 = a bad option), so the gate never goes blind by skipping
PKILL_STUB = None if shutil.which("pkill") else ("case \"$1\" in --*) echo \"pkill: unrecognized option '$1'\" >&2; "
                                                 "exit 2;; esac\nexit 1\n")


def _pkill() -> dict[str, str]:
    return {"pkill": PKILL_STUB} if PKILL_STUB else {}


def test_the_live_line_is_guarded_on_the_frame_and_said():
    frame = _frame(f"{LIVE}\n{AFTER}")
    body = _remote_body(frame["files"][0]["content"])
    assert body.startswith("set -e"), body[:80]                  # a writing block: the template's strict line
    assert GUARDED in body and LIVE not in body
    notes = [n for n in frame["engine_fixed"] if "nothing to stop" in n]
    assert len(notes) == 1 and "qq-bench-runner" in notes[0] and "set -e" in notes[0], frame["engine_fixed"]


def test_the_repaired_script_runs_on_when_pkill_matches_nothing(tmp_path):
    """The REAL pkill, a pattern nothing matches (exit 1): the restart after it runs."""
    out = _run(_remote_body(_frame(f"{LIVE}\n{AFTER}")["files"][0]["content"]), tmp_path, _pkill())
    assert out.returncode == 0, out.stdout + out.stderr
    assert "docker exec ollama ollama ps" in _log(tmp_path) and "systemd-run --unit=bench-new" in _log(tmp_path)


def test_unrepaired_the_same_script_stops_at_pkill(tmp_path):
    """The defect is real: the drafted line under the template's `set -e` ends the step at `pkill` (exit 1)."""
    body = _remote_body(_render(f"{LIVE}\n{AFTER}").split("### /tmp/in_vm_110.sh\n```bash\n", 1)[1])
    assert LIVE in body
    out = _run(body, tmp_path, _pkill())
    assert out.returncode == 1 and _log(tmp_path) == ""


def test_a_real_pkill_error_still_stops_the_script(tmp_path):
    """The REAL pkill with a bad option exits 2: only 1 is tolerated, so the script still ends there (its status is
    the guard's 1, and pkill's own error is on stderr for the operator)."""
    body = _repaired("#!/bin/bash\nset -euo pipefail\npkill --qq-no-such-option qq-x; docker exec after\n")
    assert "pkill --qq-no-such-option qq-x || [ $? -eq 1 ]; docker exec after" in body
    alone = _run("pkill --qq-no-such-option qq-x", tmp_path, _pkill())
    assert alone.returncode == 2                                 # the error is pkill's 2, not its no-match 1
    out = _run(body, tmp_path, _pkill())
    assert out.returncode != 0 and _log(tmp_path) == "", out.stdout + out.stderr
    assert "qq-no-such-option" in out.stderr


@pytest.mark.parametrize("line", ["systemctl stop jellyfin", "sudo systemctl stop palworld.service 2>/dev/null",
                                  "systemctl stop lm-bench2; docker exec ollama ollama ps"])
def test_systemctl_stop_is_left_to_fail_loudly(tmp_path, line):
    """`systemctl stop`'s 5 ("not loaded") is also a MISSPELLED unit's: a stop-then-edit step (jellyfin's DB) must
    not go on editing while the real service runs, so the stop is left as written and still ends the script."""
    script = f"#!/bin/bash\nset -e\n{line}\ndocker start bench\n"
    files, notes = sr.repair_no_op_stops_under_set_e([{"path": "/tmp/x.sh", "content": script}])
    assert notes == [] and files[0]["content"] == script
    out = _run(script, tmp_path, {"systemctl": "echo 'Unit jellyfn.service not loaded.' >&2\nexit 5\n",
                                  "sudo": 'exec "$@"\n'})
    assert out.returncode == 5 and "docker start bench" not in _log(tmp_path)


def test_a_real_killall_error_still_stops_the_script(tmp_path):
    """Only killall's 1 is tolerated: a stub that fails otherwise (a bad signal, exit 2 here) still ends the script."""
    body = _repaired("#!/bin/bash\nset -e\nkillall -s NOPE bench\ndocker start next\n")
    assert "killall -s NOPE bench || [ $? -eq 1 ]" in body
    out = _run(body, tmp_path, {"killall": "echo 'killall: unknown signal; killall -l lists signals.' >&2\nexit 2\n"})
    assert out.returncode != 0 and _log(tmp_path) == "", out.stdout + out.stderr


@pytest.mark.parametrize("err, runs_on", [("Error response from daemon: No such container: eager_turing", True),
                                          ("Cannot connect to the Docker daemon at unix:///var/run/docker.sock.", False)])
def test_docker_stop_tolerates_no_such_container_only(tmp_path, err, runs_on):
    body = _repaired("#!/bin/bash\nset -euo pipefail\ndocker stop -t 5 eager_turing\nsystemd-run --unit=next true\n")
    assert "docker stop -t 5 eager_turing || case" in body
    out = _run(body, tmp_path, {"docker": f"echo '{err}' >&2\nexit 1\n"})
    assert (out.returncode == 0) is runs_on and ("systemd-run --unit=next" in _log(tmp_path)) is runs_on


def test_the_whole_live_draft_gets_both_repairs_once_each():
    """LM14's draft opened with §17.1972's `a; b; true` line, then the pkill line: the group is left to §17.1972
    (its stops already run under `|| true`), the pkill gets its own guard, one note each."""
    first = "systemctl stop bench-old 2>/dev/null; docker stop eager_turing 2>/dev/null; true"
    frame = _frame(f"{first}\n{LIVE}\n{AFTER}")
    body = _remote_body(frame["files"][0]["content"])
    assert "{ systemctl stop bench-old 2>/dev/null; docker stop eager_turing 2>/dev/null; } || true" in body
    assert GUARDED in body and "[ $? -eq 5 ]" not in body
    assert len([n for n in frame["engine_fixed"] if "nothing to stop" in n]) == 1
    assert len([n for n in frame["engine_fixed"] if "`{ …; } || true`" in n]) == 1


def test_killall_and_a_trailing_comment():
    body = _repaired("#!/bin/bash\nset -e\nsudo killall -w bench  # the old one\n")
    assert "sudo killall -w bench || [ $? -eq 1 ]  # the old one" in body


def test_what_is_not_a_bare_stop_under_set_e_is_left_alone():
    body = ("#!/bin/bash\npkill a\n"                            # no set -e yet: its exit ends nothing
            "set -e\n"
            "tee /root/inner.sh <<'EOF'\npkill b\nEOF\n"        # a file's content, not run under this set -e
            "pgrep -f bench\n"                                  # a check that the thing runs
            "pkill -0 bench\n"                                  # signal 0 is a check too
            "systemctl disable --now old\n"                     # disable of a missing unit is the generic exit 1
            "docker rm -f old\n"                                # already exits 0 when it is missing
            "docker stop a b\n"                                 # two names: one inspect cannot say which was missing
            "rm /tmp/stale\n"                                   # not a stop with a no-op exit
            "pkill c || true\n"                                 # already guarded
            "pkill d && echo gone\n"                            # in a list
            "pkill e | tee /tmp/l\n"                            # in a pipe
            "pkill f &\n"                                       # backgrounded
            "! pkill g\n"                                       # negated
            "if pkill h; then echo; fi\n"                       # a condition
            "{ pkill i; docker stop j; } || true\n"             # a guarded group (§17.1972's repair)
            "x=$(pkill k)\n"
            "echo 'pkill l; done'\n"                            # inside a quote
            "a && \\\n  pkill m\n"                              # a continuation
            "# pkill n\n"
            "set +e\npkill o\n")                                # errexit off again
    files, notes = sr.repair_no_op_stops_under_set_e([{"path": "/tmp/x.sh", "content": body}])
    assert notes == [] and files[0]["content"] == body


def test_the_engines_own_templates_need_no_repair():
    truth = mt.GuestTruth(gid="110", kind="vm", status="running", agent=True, key_known_by="ADD26")
    seen = 0
    for name in dir(rt):
        tpl = getattr(rt, name)
        if not isinstance(tpl, rt.Template):
            continue
        try:
            runbook = rt.render(tpl, rt.values_for(tpl, NODE, truth, ENV, {"REMOTE_COMMANDS": "touch /root/x"}))
        except Exception:                                    # a template that needs a subject this node lacks
            continue
        files = [f for f in sr.file_writes(runbook) if f["path"].endswith(".sh")]
        _, notes = sr.repair_no_op_stops_under_set_e(files)
        assert notes == [], (name, notes)
        seen += 1
    assert seen >= 3
