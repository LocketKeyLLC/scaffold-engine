"""§17.1972 — a line written ``a; b; true`` ignores its failures under ``set -e`` too.

Live (job 0fb4ea7f LM14, 2026-11-20): "restart the benchmark A/B" drafted ``systemctl stop lm-bench2 2>/dev/null;
docker stop vibrant_hugle 2>/dev/null; true`` into the in-guest remote script, which opens with ``set -e``; a stop
that failed (the very case the ``; true`` was written for) would have ended the step with nothing restarted. The
engine now writes such a line ``{ a; b; } || true`` and says so on the frame (``engine_fixed``).
"""
from __future__ import annotations

import os
import pathlib
import re
import subprocess

from app.modules import machine_truth as mt
from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

NODE = {"node_key": "LM14", "title": "VM 110: restart the benchmark A/B",
        "description": "Restart the benchmark inside VM 110."}
ENV = {"profile": "root@pve", "system_state": {"110": {"kind": "vm", "attrs": {"name": "bench-vm"}}}}
LIVE = "systemctl stop bench-old 2>/dev/null; docker stop eager_turing 2>/dev/null; true"   # the live shape, names made up
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


def _run(body: str, tmp: pathlib.Path) -> subprocess.CompletedProcess:
    """The remote script, run for real: `systemctl stop` fails (the unit is gone), `docker` and `systemd-run` log."""
    b = tmp / "bin"; b.mkdir(exist_ok=True)
    (b / "systemctl").write_text("#!/bin/sh\necho \"Unit $3.service not loaded.\" >&2\nexit 5\n")
    (b / "docker").write_text(f"#!/bin/sh\necho \"docker $*\" >> {tmp}/log\n")
    (b / "systemd-run").write_text(f"#!/bin/sh\necho \"systemd-run $*\" >> {tmp}/log\n")
    for f in b.iterdir():
        f.chmod(0o755)
    p = tmp / "remote.sh"; p.write_text(body + "\n")
    assert subprocess.run(["bash", "-n", str(p)]).returncode == 0
    return subprocess.run(["bash", str(p)], capture_output=True, text=True, timeout=30,
                          env={**os.environ, "PATH": f"{b}:{os.environ['PATH']}"})


def test_the_live_line_is_repaired_on_the_frame_and_said():
    frame = _frame(f"{LIVE}\n{AFTER}")
    body = _remote_body(frame["files"][0]["content"])
    assert body.startswith("set -e"), body[:80]                  # a writing block: the template's strict line
    assert "{ systemctl stop bench-old 2>/dev/null; docker stop eager_turing 2>/dev/null; } || true" in body
    assert LIVE not in body
    notes = [n for n in frame["engine_fixed"] if "set -e" in n and "|| true" in n]
    assert len(notes) == 1 and "bench-old" in notes[0], frame["engine_fixed"]


def test_the_repaired_script_runs_past_a_failing_first_command(tmp_path):
    out = _run(_remote_body(_frame(f"{LIVE}\n{AFTER}")["files"][0]["content"]), tmp_path)
    log = (tmp_path / "log").read_text()
    assert out.returncode == 0, out.stdout + out.stderr
    assert "docker stop eager_turing" in log and "systemd-run --unit=bench-new" in log   # the rest of the line, and the step


def test_unrepaired_the_same_script_stops_at_the_first_command(tmp_path):
    """The defect is real: the drafted line under the template's `set -e` ends the step at `systemctl stop`."""
    body = _remote_body(_render(f"{LIVE}\n{AFTER}").split("### /tmp/in_vm_110.sh\n```bash\n", 1)[1])
    assert LIVE in body
    out = _run(body, tmp_path)
    assert out.returncode == 5 and not (tmp_path / "log").exists()


def test_a_colon_is_the_same_no_op():
    files, notes = sr.repair_ignored_failures_under_set_e(
        [{"path": "/tmp/x.sh", "content": "#!/bin/bash\nset -euo pipefail\nrm /tmp/a; pkill -f 'a b'; :\necho ok\n"}])
    assert "{ rm /tmp/a; pkill -f 'a b'; } || true" in files[0]["content"] and len(notes) == 1


def test_what_is_not_a_statement_under_set_e_is_left_alone():
    body = ("#!/bin/bash\nx; true\n"                           # no set -e yet: `; true` already works
            "set -e\n"
            "tee /root/inner.sh <<'EOF'\nstop-a; true\nEOF\n"   # a file's content, not run under this set -e
            "if probe; then act; true\nfi\n"                    # a block head: wrapping it breaks the if
            "a && b \\\n  c; true\n"                            # a continuation line
            "echo 'one; true\nhalf'; true\n"                    # an open quote
            "only || true; true\n"                              # one command, already `|| true`
            "# note; true\n"
            "set +e\nlater; true\n")                            # errexit off again
    files, notes = sr.repair_ignored_failures_under_set_e([{"path": "/tmp/x.sh", "content": body}])
    assert notes == [] and files[0]["content"] == body


def test_a_read_only_block_is_not_under_set_e_and_is_left_alone():
    frame = _frame("grep -rIl -e 'needle' /etc/hosts 2>/dev/null; true\ncat /etc/os-release")
    body = _remote_body(frame["files"][0]["content"])
    # §17.2060 — each statement of a read is its own section: `a; true` is the two lines `a` and `true`, unrepaired
    assert body.startswith("set +e") and "\ngrep -rIl -e 'needle' /etc/hosts 2>/dev/null\n" in body and "\ntrue\n" in body
    assert not [n for n in frame["engine_fixed"] if "`{ …; } || true`" in n]


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
        _, notes = sr.repair_ignored_failures_under_set_e(files)
        assert notes == [], (name, notes)
        seen += 1
    assert seen >= 3
