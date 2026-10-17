"""§17.1625 — a step whose work runs inside a guest has its own "Done when" checks run inside that guest.

Live (job 0fb4ea7f T18, 2026-10-16): the ssh template's checks are host-side (`qm status 110`), so §17.1470 added the
step's own `head -n 40 /opt/scaffold-engine/PLACEMENT.md` bare and it read the HOST, where the file does not exist;
on the agent and LXC templates the same check was silently dropped. And RD1/RD3's checks were never read at all: a
short description ("From D9.") hid the prompt's "Done when".
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
from pathlib import Path

from app.modules import runbook_templates as rt
from app.modules.assist_state_check import read_only_command
from app.modules.operator_steps import checks
from app.modules.supervised_runs import with_own_checks

CHECK = "head -n 40 /opt/scaffold-engine/PLACEMENT.md"
STEP = {"title": "Document placement and setup", "description": "From D9.",
        "prompt_template": f"Inside VM 110: write the placement record.\nDone when `{CHECK}` prints it."}
SSH_RUN = ['MASS_PASSWORD="$MASS_PASSWORD" GUEST_USER="<AI_USER>" bash /tmp/in_vm_110.sh start',
           'MASS_PASSWORD="$MASS_PASSWORD" GUEST_USER="<AI_USER>" bash /tmp/in_vm_110.sh last']


def test_a_short_description_does_not_hide_the_prompt_s_checks():
    assert checks(STEP) == [CHECK]
    assert checks({"description": f"Done when `{CHECK}` prints it.", "prompt_template": ""}) == [CHECK]


def test_the_ssh_template_s_check_runs_inside_the_guest_not_on_the_host():
    out = with_own_checks(STEP, ["qm status 110"], SSH_RUN)
    assert CHECK not in out, "never bare: it would read the host"
    assert out == ["qm status 110",
                   f"MASS_PASSWORD=\"$MASS_PASSWORD\" GUEST_USER=\"<AI_USER>\" bash /tmp/in_vm_110.sh check '{CHECK}'"]


def test_the_agent_and_lxc_templates_wrap_it_instead_of_dropping_it():
    agent = with_own_checks(STEP, ['qm guest exec 110 -- bash -c "df -h /"'], ["bash /tmp/in_vm_110_agent.sh start"])
    assert agent[-1] == f"qm guest exec 110 -- bash -c '{CHECK}'" and read_only_command(agent[-1])
    ct = with_own_checks(STEP, ['pct exec 105 -- bash -c "true"'], ["bash /tmp/in_ct_105.sh start"])
    assert ct[-1] == f"pct exec 105 -- bash -c '{CHECK}'" and read_only_command(ct[-1])


def test_a_hypervisor_check_stays_on_the_host_and_a_plain_block_is_unchanged():
    host = {"prompt_template": "Done when `qm status 110` shows running."}
    assert with_own_checks(host, ["qm config 110"], SSH_RUN) == ["qm config 110", "qm status 110"]
    # no in-guest template: §17.1470's rule as it was (bare check added when every check is host-level)
    assert with_own_checks(STEP, ["systemctl is-active nginx"], ["systemctl restart nginx"])[-1] == CHECK


def _script() -> str:
    body = rt.render(rt.REACH_VM_SSH_AND_RUN, {"GID": "110", "STEP": "T18", "REMOTE_COMMANDS": "true",
                                              "GUEST_USER_NAME": "AI_USER", "NEEDS_KEY": "yes"})
    return re.search(r"### /tmp/in_vm_110.sh\n```bash\n(.*?)\n```", body, re.S).group(1)


def _fake(bindir: Path, name: str, body: str) -> None:
    f = bindir / name
    f.write_text("#!/bin/bash\n" + body)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)


def test_bash_level_the_check_phase_reads_inside_and_changes_nothing(tmp_path):
    script = _script()
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0
    ipfile = "/tmp/in_vm_110.ip"
    script = script.replace(ipfile, str(tmp_path / "ip"))
    (tmp_path / "s.sh").write_text(script)
    bindir, log = tmp_path / "bin", tmp_path / "calls.log"
    bindir.mkdir()
    # ssh echoes what it would run and reads sudo's password off stdin; every setup tool is a tripwire
    _fake(bindir, "ssh", f'echo "ssh $*" >> {log}; read -r pw; echo "pw=$pw" >> {log}; echo "# Scaffold Engine Placement"\n')
    for n in ("qm", "ssh-copy-id", "sshpass", "ip", "nmap", "apt-get"):
        _fake(bindir, n, f'echo "{n} $*" >> {log}\nexit 0\n')
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "MASS_PASSWORD": "s3cret", "GUEST_USER": "aedefruscio"}

    r = subprocess.run(["bash", str(tmp_path / "s.sh"), "check", CHECK], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 1 and "no address recorded for VM 110" in r.stdout, "no address: refuse, do not go find one"
    assert not log.exists(), "nothing ran: no qm start, no address scan, no key copy"

    (tmp_path / "ip").write_text("192.168.1.129\n")
    r = subprocess.run(["bash", str(tmp_path / "s.sh"), "check", CHECK], env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0 and "# Scaffold Engine Placement" in r.stdout
    calls = log.read_text()
    assert "aedefruscio@192.168.1.129" in calls and "sudo -S -p '' bash -c" in calls and "PLACEMENT.md" in calls
    assert "pw=s3cret" in calls, "the password goes to sudo on stdin, never on the command line"
    assert "s3cret" not in calls.split("pw=s3cret")[0]
    assert not re.search(r"^(qm|ssh-copy-id|sshpass|ip|nmap|apt-get) ", calls, re.M)
