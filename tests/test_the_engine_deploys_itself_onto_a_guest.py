"""§17.1618 — the engine deploys ITSELF onto a guest: an engine-owned template + scripts/deploy_on_guest.sh.

Live (job 0fb4ea7f T14 "Deploy engine services", 2026-10-16): the drafted deploy was not this engine at all (the OWUI
pipelines image as the "orchestrator", invented env names, the homelab password as the database password). The engine
had no way to put itself on a machine; now a step that says so is drafted from the engine's own repo and bootstrap.
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import textwrap
from pathlib import Path

from app.modules import runbook_templates as rt

ROOT = Path(__file__).resolve().parent.parent
T14 = {"node_key": "T14", "title": "VM 110: deploy engine services", "description": "",
       "prompt_template": textwrap.dedent("""\
           Inside VM 110: deploy the scaffold engine's services. Settings:
           OLLAMA_COMPOSE_DIR=/opt/ollama
           SCAFFOLD_LAN_UI=1
           SCAFFOLD_LAN_ADDRESS=10.9.8.129
           MODEL_CODER=hf.co/peculiar-ragdoll/Cyber-Tiel-Coder-35B-A3B-GGUF:UD-IQ4_XS
           SCAFFOLD_API_KEY=should-never-travel
           """)}
TRUTH = type("T", (), {"gid": "110", "kind": "vm", "status": "running", "agent": False})()


def test_a_deploy_step_selects_the_engine_s_own_template():
    assert rt.select_template(T14, TRUTH) is rt.DEPLOY_SCAFFOLD_ENGINE
    other = {"title": "VM 110: install Docker", "prompt_template": "apt-get install docker"}
    assert rt.select_template(other, TRUTH) is rt.REACH_VM_SSH_AND_RUN


def test_settings_come_from_the_step_and_a_secret_name_never_travels():
    s = rt.deploy_settings(T14)
    assert "MODEL_CODER=hf.co/peculiar-ragdoll/Cyber-Tiel-Coder-35B-A3B-GGUF:UD-IQ4_XS" in s
    assert "OLLAMA_COMPOSE_DIR=/opt/ollama" in s and not any(x.startswith("SCAFFOLD_API_KEY") for x in s)


def test_the_rendered_runbook_clones_at_the_ref_runs_the_script_and_parses(monkeypatch):
    monkeypatch.setenv("SCAFFOLD_DEPLOY_REF", "some-branch")
    body = rt.render(rt.DEPLOY_SCAFFOLD_ENGINE, rt.values_for(rt.DEPLOY_SCAFFOLD_ENGINE, T14, TRUTH, {}, {}))
    assert "checkout --quiet --detach origin/some-branch" in body
    assert "bash /opt/scaffold-engine/scripts/deploy_on_guest.sh /root/scaffold-deploy.env" in body
    assert "should-never-travel" not in body
    script = re.search(r"### /tmp/in_vm_110.sh\n```bash\n(.*?)\n```", body, re.S).group(1)
    assert subprocess.run(["bash", "-n"], input=script, text=True).returncode == 0
    runs = [ln for ln in body.splitlines() if "bash /tmp/in_vm_110.sh" in ln and ln.startswith("MASS_PASSWORD")]
    assert runs[0].endswith(" start") and runs[-1].endswith(" last") and len(runs) == 32


def _fake(bindir: Path, name: str, body: str) -> None:
    f = bindir / name
    f.write_text("#!/bin/bash\n" + body)
    f.chmod(f.stat().st_mode | stat.S_IEXEC)


def test_bash_level_the_deploy_script_runs_twice_and_keeps_its_secrets(tmp_path):
    repo, bindir, units, ollama = tmp_path / "repo", tmp_path / "bin", tmp_path / "units", tmp_path / "ollama"
    for d in (repo / "scripts", bindir, units, ollama):
        d.mkdir(parents=True)
    for f in ("deploy_on_guest.sh", "scaffold_lan_gate.sh", "scaffold-lan-gate.service"):
        shutil.copy(ROOT / "scripts" / f, repo / "scripts" / f)
    (repo / "scripts" / "bootstrap.sh").write_text(
        "#!/usr/bin/env bash\nprintf 'SCAFFOLD_API_KEY=generated\\nMODEL_ROUTER=qwen3:4b\\n' > .env\n")
    (ollama / "docker-compose.yml").write_text("services: {}\n")
    log = tmp_path / "calls.log"
    _fake(bindir, "docker", f'echo "docker $*" >> {log}\ncase "$1 $2" in\n'
                            f'  "network inspect") [ -f {tmp_path}/net ] || exit 1;;\n'
                            f'  "network create") touch {tmp_path}/net;;\n'
                            '  "exec ollama") [ "$4" = list ] && printf "NAME ID\\nqwen3:4b x\\n";;\nesac\nexit 0\n')
    for n in ("curl", "systemctl"):
        _fake(bindir, n, f'echo "{n} $*" >> {log}\nexit 0\n')
    _fake(bindir, "iptables", f'echo "iptables $*" >> {log}\n[ "$1" = -C ] && exit 1\nexit 0\n')
    settings = tmp_path / "settings.env"
    settings.write_text(f"OLLAMA_COMPOSE_DIR={ollama}\nSCAFFOLD_LAN_UI=1\nSCAFFOLD_LAN_ADDRESS=10.9.8.129\n"
                        "SCAFFOLD_LAN_ALLOW=10.9.8.0/24\nMODEL_CODER=hf.co/x/coder:Q4\nCOMPOSE_PROFILES=owui\n")
    env = {**os.environ, "PATH": f"{bindir}:{os.environ['PATH']}", "SCAFFOLD_SYSTEMD_DIR": str(units)}
    for _ in range(2):
        r = subprocess.run(["bash", "scripts/deploy_on_guest.sh", str(settings)], cwd=repo, env=env,
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stdout + r.stderr
    dotenv = (repo / ".env").read_text()
    assert "SCAFFOLD_API_KEY=generated" in dotenv and "MODEL_CODER=hf.co/x/coder:Q4" in dotenv
    assert "COMPOSE_FILE=docker-compose.yml:docker-compose.lan.yml:docker-compose.lan-ui.yml" in dotenv
    assert "OLLAMA_COMPOSE_DIR" not in dotenv
    calls = log.read_text()
    assert calls.count("network create") == 1, "ai-network is created once"
    assert "ollama pull hf.co/x/coder:Q4" in calls and "ollama pull qwen3:4b" not in calls
    assert "--ctorigdstport 3000" in calls and "--ctorigdstport 8000" in calls
    assert (ollama / "docker-compose.override.yml").read_text().count("ai-network") == 1
    assert str(repo) in (units / "scaffold-lan-gate.service").read_text()
