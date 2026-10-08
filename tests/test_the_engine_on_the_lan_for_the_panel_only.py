"""§17.1418 — the engine on the LAN for one client; a developed step is told the engine's real address
and needs a check of its own.

Live, 2026-10-07, ADD124 ("reach the engine's own surface"): the developed route pointed at
`127.0.0.1:3000` -- the container itself, on an invented port -- and the step's only check was the
panel unit's `is-active`, which the delivery makes true. Measured: the engine publishes 8000 on its
host's loopback only, so CT 111 could not have reached it at any address. Operator decision: publish
on the LAN address for the panel ONLY (a DOCKER-USER allow-list, installed before Docker starts).
"""
from __future__ import annotations

import inspect
import json
import os
import pathlib
import subprocess

import pytest

from app.modules import develop as dv
from app.modules import execution_agent

ROOT = pathlib.Path(__file__).parents[1]
FIX = pathlib.Path(__file__).parent / "fixtures"
ADD122 = json.loads((FIX / "add122_node_2026_10_07.json").read_text())
ADD123 = json.loads((FIX / "add123_node_2026_10_07.json").read_text())
ADD124 = json.loads((FIX / "add124_node_2026_10_07.json").read_text())
HOST = dv.Host(guest="111", vm=False, workdir="/opt/control-panel-backend", unit="control-panel.service")


# ── no check of its own, no run ─────────────────────────────────────────────

def test_a_step_whose_only_check_is_is_active_is_refused():
    refs = dv.own_check_refusal(ADD124, HOST)
    assert len(refs) == 1 and dv.NO_OWN_CHECK in refs[0]["why"]
    assert "pct exec 111 -- curl -s http://127.0.0.1:<port>/api/<route>" in refs[0]["why"]


def test_steps_with_their_own_check_are_not():
    assert dv.own_check_refusal(ADD122, HOST) == []          # a GET read check
    assert dv.own_check_refusal(ADD123, HOST) == []          # a POST acceptance check


def test_the_loop_refuses_before_spending_a_round():
    src = inspect.getsource(execution_agent._pause_for_decision)
    i, j = src.index("develop.own_check_refusal(run_node, host)"), src.index("await develop.read_workspace(spec, host)")
    assert i < j


# ── the engine's own address ────────────────────────────────────────────────

def test_an_engine_step_is_told_the_engine_is_unreachable_when_it_is(monkeypatch):
    monkeypatch.delenv("SCAFFOLD_LAN_URL", raising=False)
    assert "is NOT reachable" in dv.engine_doc(ADD124)


def test_an_engine_step_is_told_the_lan_address(monkeypatch):
    monkeypatch.setenv("SCAFFOLD_LAN_URL", "http://192.168.1.43:8000")
    monkeypatch.setenv("SCAFFOLD_LAN_ALLOW", "192.168.1.25")
    doc = dv.engine_doc(ADD124)
    assert "http://192.168.1.43:8000/health" in doc and "192.168.1.25" in doc and "Never 127.0.0.1" in doc


def test_a_step_not_about_the_engine_is_told_nothing(monkeypatch):
    monkeypatch.setenv("SCAFFOLD_LAN_URL", "http://192.168.1.43:8000")
    assert dv.engine_doc(ADD122) == ""


def test_the_loop_puts_it_in_the_facts():
    assert "develop.engine_doc(run_node)" in inspect.getsource(execution_agent._pause_for_decision)


# ── the gate, at bash level ─────────────────────────────────────────────────

@pytest.fixture
def stub(tmp_path):
    b = tmp_path / "bin"
    b.mkdir()
    (b / "iptables").write_text('#!/bin/bash\necho "iptables $*" >> "$LOG"\n'
                                'case "$1" in -C) [ -f "$STATE" ] && exit 0 || exit 1;; -I) touch "$STATE";; esac\n')
    (b / "iptables").chmod(0o755)
    env = {**os.environ, "PATH": f"{b}:{os.environ['PATH']}", "LOG": str(tmp_path / "log"), "STATE": str(tmp_path / "st")}
    return tmp_path, env


def _run(env, envfile):
    return subprocess.run(["bash", str(ROOT / "scripts/scaffold_lan_gate.sh"), str(envfile)],
                          env=env, capture_output=True, text=True)


def test_the_gate_installs_once_and_only_lets_the_allowed_client_in(stub):
    tmp, env = stub
    f = tmp / ".env"
    f.write_text('SCAFFOLD_API_KEY=x\nSCAFFOLD_LAN_ADDRESS=192.168.1.43\nSCAFFOLD_LAN_ALLOW="192.168.1.25"\n')
    assert _run(env, f).returncode == 0 and _run(env, f).returncode == 0
    log = (tmp / "log").read_text().splitlines()
    inserts = [ln for ln in log if " -I " in f" {ln} "]
    assert inserts == ["iptables -I DOCKER-USER 1 -p tcp -m conntrack --ctdir ORIGINAL --ctorigdst 192.168.1.43 "
                       "--ctorigdstport 8000 ! -s 192.168.1.25 -j DROP"]


def test_the_rule_judges_only_packets_toward_the_engine():
    """§17.1418b — measured: without `--ctdir ORIGINAL` the engine's REPLIES (source 172.18.x) matched
    `! -s ALLOW` on the shared original tuple, and the allowed client timed out too."""
    src = (ROOT / "scripts/scaffold_lan_gate.sh").read_text()
    rule = next(ln for ln in src.splitlines() if ln.startswith("RULE=("))
    assert "--ctdir ORIGINAL" in rule


def test_the_earlier_rule_is_removed_on_upgrade(tmp_path):
    """The rule already on the operator's host is the reply-dropping one: rerunning the gate must
    delete it, not stack the new rule beside it."""
    b = tmp_path / "bin"
    b.mkdir()
    # a stateful stub: rules live in a file, -C/-I/-D act on it
    stub_lines = [
        "#!/bin/bash",
        'R="$RULES"',
        'case "$1" in',
        "  -N) exit 0;;",
        '  -C) grep -qxF -- "${*:3}" "$R" 2>/dev/null;;',
        '  -D) grep -vxF -- "${*:3}" "$R" > "$R.t"; mv "$R.t" "$R";;',
        '  -I) echo "${*:4}" >> "$R";;',
        "esac",
    ]
    (b / "iptables").write_text("\n".join(stub_lines) + "\n")
    (b / "iptables").chmod(0o755)
    rules = tmp_path / "rules"
    rules.write_text("-p tcp -m conntrack --ctorigdst 192.168.1.43 --ctorigdstport 8000 ! -s 192.168.1.25 -j DROP\n")
    f = tmp_path / ".env"
    f.write_text("SCAFFOLD_LAN_ADDRESS=192.168.1.43\nSCAFFOLD_LAN_ALLOW=192.168.1.25\n")
    env = {**os.environ, "PATH": f"{b}:{os.environ['PATH']}", "RULES": str(rules)}
    r = subprocess.run(["bash", str(ROOT / "scripts/scaffold_lan_gate.sh"), str(f)], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "removed the earlier rule" in r.stdout
    assert rules.read_text().splitlines() == [
        "-p tcp -m conntrack --ctdir ORIGINAL --ctorigdst 192.168.1.43 --ctorigdstport 8000 ! -s 192.168.1.25 -j DROP"]


def test_the_gate_does_nothing_when_the_overlay_is_off(stub):
    tmp, env = stub
    f = tmp / ".env"
    f.write_text("SCAFFOLD_API_KEY=x\n")
    r = _run(env, f)
    assert r.returncode == 0 and "nothing to gate" in r.stdout and not (tmp / "log").exists()


def test_bash_n():
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts/scaffold_lan_gate.sh")]).returncode == 0


# ── the overlay and the unit ────────────────────────────────────────────────

def test_the_overlay_is_off_unless_named_and_binds_one_address():
    y = (ROOT / "docker-compose.lan.yml").read_text()
    assert '"${SCAFFOLD_LAN_ADDRESS:?set SCAFFOLD_LAN_ADDRESS in .env}:8000:8000"' in y
    assert "SCAFFOLD_LAN_URL: http://${SCAFFOLD_LAN_ADDRESS}:8000" in y
    base = (ROOT / "docker-compose.yml").read_text()
    assert '- "127.0.0.1:8000:8000"' in base and "SCAFFOLD_LAN_ADDRESS" not in base


def test_the_unit_runs_before_docker():
    u = (ROOT / "scripts/scaffold-lan-gate.service").read_text()
    assert "Before=docker.service" in u and "WantedBy=docker.service" in u and "Type=oneshot" in u
