"""§17.1744 — a cold VM is booted in its own phases, each inside the runner's 180 s.

Live (job 0fb4ea7f RD28, 2026-10-22, after a host crash): VM 110 was stopped; `start` had to boot it and reach it before
launching anything, and the runner killed it at 180 s ("timed out after 180s") with nothing started."""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest

from app.modules import machine_truth as mt
from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr

NODE = {"node_key": "RD28", "title": "VM 110: redeploy engine services", "description": "Redeploy inside VM 110."}
ENV = {"profile": "root@pve", "system_state": {"110": {"kind": "vm", "attrs": {"name": "ai-vm"}}}}


def _render(tpl=rt.REACH_VM_SSH_AND_RUN):
    truth = mt.GuestTruth(gid="110", kind="vm", status="stopped", agent=False, key_known_by="ADD26")
    vals = rt.values_for(tpl, NODE, truth, ENV, {"REMOTE_COMMANDS": "true"})
    return rt.render(tpl, vals)


@pytest.mark.smoke
def test_boot_phases_come_first_and_the_deploy_keeps_them():
    phases = [c.split()[-1] for c in sr.runbook_commands(_render())]
    assert phases[:4] == ["boot", "boot", "boot", "start"] and phases[-1] == "last"
    dep = [c.split()[-1] for c in rt.DEPLOY_SCAFFOLD_ENGINE.run]
    assert dep[:4] == ["boot", "boot", "boot", "start"] and dep.count("wait") == 30 and dep[-1] == "last"


def _fake_bin(tmp: pathlib.Path, ssh_ok_after: int) -> dict:
    b = tmp / "bin"; b.mkdir()
    (tmp / "state").write_text("stopped")
    (tmp / "ssh_tries").write_text("0")
    (b / "qm").write_text(f"""#!/bin/sh
case "$1" in
  status) echo "status: $(cat {tmp}/state)";;
  start) echo running > {tmp}/state;;
  config) echo "net0: virtio=BC:24:11:00:00:01,bridge=vmbr0";;
esac
""")
    (b / "ssh").write_text(f"""#!/bin/sh
n=$(( $(cat {tmp}/ssh_tries) + 1 )); echo $n > {tmp}/ssh_tries
[ "$(cat {tmp}/state)" = running ] && [ "$n" -ge {ssh_ok_after} ]
""")
    (b / "sleep").write_text("#!/bin/sh\nexit 0\n")
    for f in b.iterdir():
        f.chmod(0o755)
    return {**os.environ, "PATH": f"{b}:{os.environ['PATH']}", "MASS_PASSWORD": "x", "GUEST_USER": "u"}


def _script(tmp: pathlib.Path) -> pathlib.Path:
    body = sr.file_writes(_render())[0]["content"].replace("/tmp/in_vm_110", str(tmp / "in_vm_110"))
    p = tmp / "in_vm_110.sh"; p.write_text(body)
    (tmp / "in_vm_110.ip").write_text("192.0.2.129\n")
    assert subprocess.run(["bash", "-n", str(p)]).returncode == 0
    return p


def test_a_cold_vm_is_started_and_waited_for(tmp_path):
    env = _fake_bin(tmp_path, ssh_ok_after=3)
    out = subprocess.run(["bash", str(_script(tmp_path)), "boot"], capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0, out.stdout + out.stderr
    assert "started VM 110" in out.stdout and "answers ssh at 192.0.2.129" in out.stdout


def test_a_running_vm_answers_the_first_boot_at_once(tmp_path):
    env = _fake_bin(tmp_path, ssh_ok_after=1)
    (tmp_path / "state").write_text("running")
    out = subprocess.run(["bash", str(_script(tmp_path)), "boot"], capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0 and "started" not in out.stdout and "answers ssh" in out.stdout


def test_a_vm_still_booting_hands_over_instead_of_failing(tmp_path):
    env = {**_fake_bin(tmp_path, ssh_ok_after=10**6), "BOOT_WAIT": "1"}
    out = subprocess.run(["bash", str(_script(tmp_path)), "boot"], capture_output=True, text=True, env=env, timeout=30)
    assert out.returncode == 0 and "STILL BOOTING" in out.stdout and "FAILED" not in out.stdout


def test_the_template_still_passes_every_gate():
    spec = type("S", (), {"name": "pve-runner", "headers": {}})()
    policy = {"allow": ["ANY"], "sudo": True, "helper": "24", "secrets": ["MASS_PASSWORD"], "can_write_files": True}
    frame = sr.frame_run(NODE, _render(), spec, policy, env={**ENV, "substitutions": {"PALWORLD_IP": "192.168.1.106"}})  # the template comment's address, as test_runbook_templates pins it
    assert frame["refused"] == [], [r["why"][:160] for r in frame["refused"]]
