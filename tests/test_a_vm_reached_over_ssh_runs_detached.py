"""§17.1613 — work inside a VM reached over ssh runs DETACHED and is waited on across runner calls.

Live (job 0fb4ea7f T9 "Install Ollama server", 2026-10-15): `docker compose up -d` pulling the image ran past the
runner's 180 s in the ONE ssh call this template made, and the step was cut. The container and agent templates have
been detached since §17.1317; the ssh template now has the same start | wait… | last shape.
"""
from __future__ import annotations

import re
import subprocess

from app.modules import runbook_templates as rt


def _body():
    return rt.render(rt.REACH_VM_SSH_AND_RUN, {"GID": "110", "STEP": "T9", "REMOTE_COMMANDS": "docker compose up -d",
                                              "GUEST_USER_NAME": "AI_USER", "NEEDS_KEY": "yes"})


def test_the_run_is_start_waits_last():
    runs = [ln for ln in _body().splitlines() if "bash /tmp/in_vm_110.sh" in ln and ln.startswith("MASS_PASSWORD")]
    assert [r.rsplit(" ", 1)[1] for r in runs] == ["start"] + ["wait"] * 7 + ["last"]


def test_the_script_parses_and_runs_the_work_as_a_unit():
    script = re.search(r"### /tmp/in_vm_110.sh\n```bash\n(.*?)\n```", _body(), re.S).group(1)
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0
    assert 'UNIT="scaffold-T9"' in script and "systemd-run --unit $UNIT" in script
    assert "deadline=$(( SECONDS + 150 ))" in script, "each phase stays inside the runner's 180 s"
    assert "docker compose up -d" in script
