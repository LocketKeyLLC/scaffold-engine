"""§17.2033 — the guest password and the step's script never share a stream.

The ssh template (`REACH_VM_SSH_AND_RUN`) wrote the step's script into the guest with ONE stdin,
``"$MASS_PASSWORD\\n<script>"``, into ``sudo -S -p '' tee /root/.scaffold_step.sh``. That assumes sudo consumes the
first line. On a guest whose sudo needs no password (NOPASSWD -- a cloud image's default user) sudo reads nothing,
so `tee` wrote the PASSWORD as the script's first line: a root-owned file holding the secret, run as a command whose
"command not found" names it in the step's own error log (2026-10-12 audit ledger).

These run the rendered script for real under bash, with `ssh` standing in for the network (it runs the remote
command in a scratch "guest" home), `sudo` behaving as either kind of guest, and `systemd-run` running the unit.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from app.modules import machine_truth as mt
from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

PW = "s3cret-pw-2033"
NODE = {"node_key": "RD9", "title": "VM 110: write the step marker", "description": "Inside VM 110."}
ENV = {"profile": "root@pve", "system_state": {"110": {"kind": "vm", "attrs": {"name": "ai-vm"}}}}
#: the pre-fix line, verbatim from origin/main 55b9b7b3, so the harness is shown to see the old leak
OLD_WRITE = ('    ssh $SSHO "$USER_NAME@$IP" "sudo -S -p \'\' tee /root/.scaffold_step.sh >/dev/null" <<< "$MASS_PASSWORD\n'
             '$(cat /tmp/in_vm_110_remote.sh)" || { echo "FAILED: could not write the step\'s script into VM $GID"; exit 1; }')


def _body() -> str:
    truth = mt.GuestTruth(gid="110", kind="vm", status="running", agent=False, key_known_by="ADD26")
    vals = rt.values_for(rt.REACH_VM_SSH_AND_RUN, NODE, truth, ENV,
                         {"REMOTE_COMMANDS": "echo hello-from-the-step\ncat >/dev/null || true\necho read-nothing"})
    return sr.file_writes(rt.render(rt.REACH_VM_SSH_AND_RUN, vals))[0]["content"]


def _new_write(body: str) -> str:
    start = body.index("  start)\n") + len("  start)\n")
    return body[start:body.index("    gsu \"systemctl stop $UNIT", start)]


def _harness(tmp: pathlib.Path, body: str, sudo_mode: str) -> tuple[subprocess.CompletedProcess, pathlib.Path]:
    b, home, root = tmp / "bin", tmp / "home", tmp / "root"
    for d in (b, home, root):
        d.mkdir()
    body = body.replace("/tmp/in_vm_110", str(tmp / "in_vm_110"))
    (tmp / "in_vm_110.ip").write_text("192.0.2.129\n")
    script = tmp / "in_vm_110.sh"
    script.write_text(body)
    assert subprocess.run(["bash", "-n", str(script)]).returncode == 0
    (b / "qm").write_text("#!/bin/sh\necho 'status: running'\n")
    # the network: the LAST argument is the remote command, run by a shell in the guest user's home
    (b / "ssh").write_text(f"""#!/bin/bash
cmd="${{@: -1}}"
cmd="${{cmd//\\/root\\//{root}/}}"
cd "{home}" && exec bash -c "$cmd"
""")
    # a guest's sudo: `password` reads ONE line and checks it; `nopasswd` reads nothing
    (b / "sudo").write_text(f"""#!/bin/bash
[ "$1" = -S ] && shift; [ "$1" = -p ] && shift 2
if [ "{sudo_mode}" = password ]; then IFS= read -r pw; [ "$pw" = "{PW}" ] || {{ echo "sudo: wrong password" >&2; exit 1; }}; fi
exec "$@"
""")
    (b / "systemctl").write_text("#!/bin/sh\n[ \"$1\" = is-active ] && echo inactive\nexit 0\n")
    # the unit: its StandardOutput/StandardError files, then the command, run to the end
    (b / "systemd-run").write_text("""#!/bin/bash
out=/dev/null; err=/dev/null
while [ "$1" != bash ]; do
  case "$1" in StandardOutput=append:*) out="${1#*append:}";; StandardError=append:*) err="${1#*append:}";; esac
  shift
done
"$@" >> "$out" 2>> "$err"
""")
    (b / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for f in b.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{b}:{os.environ['PATH']}", "MASS_PASSWORD": PW, "GUEST_USER": "u"}
    run = subprocess.run(["bash", str(script), "start"], capture_output=True, text=True, env=env, timeout=60)
    return run, root


def _everything_in(root: pathlib.Path) -> str:
    return "".join(p.read_text(errors="replace") for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize("sudo_mode", ["password", "nopasswd"])
def test_the_script_lands_whole_and_the_password_nowhere(tmp_path, sudo_mode):
    run, root = _harness(tmp_path, _body(), sudo_mode)
    out = run.stdout + run.stderr
    assert run.returncode == 0, out
    step = (root / ".scaffold_step.sh").read_text()
    assert step.splitlines()[0] != PW and PW not in step, step
    assert "echo hello-from-the-step" in step
    assert "hello-from-the-step" in (root / ".scaffold_step.log").read_text()
    assert "finished (exit 0)" in out, out
    assert PW not in _everything_in(root) and PW not in out
    assert not (tmp_path / "home" / ".scaffold_step.sh").exists(), "the user's staging copy is removed"


def test_a_wrong_password_fails_the_write_loudly(tmp_path, monkeypatch):
    body = _body()
    run, root = _harness(tmp_path, body.replace('"$MASS_PASSWORD" \\\n      ||', '"wrong" \\\n      ||'), "password")
    assert run.returncode == 1 and "could not write the step's script into VM 110" in run.stdout, run.stdout
    assert not (root / ".scaffold_step.sh").exists()


def test_the_command_a_root_call_runs_reads_no_password(tmp_path):
    """`gsu` and `check` hand sudo the password on stdin; on a NOPASSWD guest the line stays unread, so the command
    itself starts with its stdin closed -- the step's `cat >/dev/null || true` above would otherwise swallow it."""
    body = _body()
    assert body.count("exec </dev/null; ") == 2, "gsu and the check phase both close the command's stdin"


def test_the_harness_sees_the_old_shape_leak(tmp_path):
    """The bite: the pre-fix single-stream write, on a NOPASSWD guest, puts the password into the script."""
    body = _body()
    old = body.replace(_new_write(body), OLD_WRITE + "\n")
    assert old != body
    run, root = _harness(tmp_path, old, "nopasswd")
    step = (root / ".scaffold_step.sh").read_text()
    assert step.splitlines()[0] == PW, step[:80]
    assert PW in (root / ".scaffold_step.err").read_text(), "the secret is in the step's own error log"
