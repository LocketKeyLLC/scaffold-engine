"""§17.1426 — a developed feature's runner-held secret reaches the machine without the engine; a check that
needs a secret runs in the approved block; service state alone never makes a developed step "already met".

Live, 2026-10-08, ADD127 ("add authentication and wire the systemd service"): recorded already met from
`systemctl is-enabled` + `is-active` while the panel answered 200 with no credential. Operator decision:
a new panel password, stored by the operator in the runner as PANEL_PASSWORD.
"""
from __future__ import annotations

import inspect
import json
import os
import pathlib
import subprocess

from app.modules import develop as dv
from app.modules import execution_agent
from app.modules import supervised_runs as sr

FIX = pathlib.Path(__file__).parent / "fixtures"
ADD127 = json.loads((FIX / "add127_node_2026_10_08.json").read_text())
HOST = dv.Host(guest="111", vm=False, workdir="/opt/control-panel-backend", unit="control-panel.service")
MARK = dv.SECRET_MARK.format(name="PANEL_PASSWORD")
CFG = {"/opt/control-panel-backend/config/auth.json": '{"user": "panel", "password": "' + MARK + '"}\n'}
HOSTILE = 'p|a&s\\s"w$rd`x`/'


def test_only_the_runner_secret_the_step_names_is_offered():
    creds = dv.credentials_for([], ["PANEL_PASSWORD", "MASS_PASSWORD", "AIRVPN_WG_CONF"], ADD127)
    assert creds == {"PANEL_PASSWORD": dv.RUNNER_HELD}
    assert "the runner's secret PANEL_PASSWORD" in dv.credentials_doc(creds, [])


def test_the_check_that_needs_the_secret_runs_approved_not_read_only():
    reads = dv.done_checks(ADD127, HOST)
    assert not any("PANEL_PASSWORD" in c for c in reads)
    assert any("-w %{http_code} http://127.0.0.1:3001/api/palworld-settings" in c for c in reads)   # the 401 read
    acc = dv.acceptance_checks(ADD127)
    assert len(acc) == 1 and 'curl -f -s' in acc[0] and '-u panel:"$PANEL_PASSWORD"' in acc[0]


def _rb():
    creds = dv.credentials_for([], ["PANEL_PASSWORD"], ADD127)
    return dv.render_delivery(ADD127, HOST, CFG, dv.done_checks(ADD127, HOST), dv.acceptance_checks(ADD127), creds)


def test_the_run_line_names_the_secret_so_the_runner_injects_it():
    cmds = sr.runbook_commands(_rb())
    assert cmds[0].startswith('PANEL_PASSWORD="$PANEL_PASSWORD" bash /tmp/scaffold-dev-ADD127--deliver.sh')
    sh = {f["path"]: f["content"] for f in sr.file_writes(_rb())}["/tmp/scaffold-dev-ADD127--deliver.sh"]
    assert 'V_PANEL_PASSWORD="${PANEL_PASSWORD:-}"' in sh and "perl -pi -e" in sh
    assert "pct exec 111 -- chmod 600 /opt/control-panel-backend/config/auth.json" in sh
    assert subprocess.run(["bash", "-n"], input=sh, text=True).returncode == 0


def test_bash_level_a_hostile_password_lands_exact_and_is_never_printed(tmp_path):
    files = {f["path"]: f["content"] for f in sr.file_writes(_rb())}
    pushed = tmp_path / "pushed"
    pushed.mkdir()
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "pct").write_text("\n".join([
        "#!/bin/bash",
        'case "$1" in',
        '  status) echo "status: running";;',
        '  push) cp "$3" "' + str(pushed) + '/$(basename "$4")";;',
        '  exec) shift 2; [ "$1" = "--" ] && shift; if [ "$1" = "systemctl" ] && [ "$2" = "is-active" ]; then echo active; fi;;',
        "esac", ""]))
    (stub / "pct").chmod(0o755)
    for path, content in files.items():
        pathlib.Path(path).write_text(content)
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}", "PANEL_PASSWORD": HOSTILE}
    r = subprocess.run(["bash", "/tmp/scaffold-dev-ADD127--deliver.sh"], env=env, capture_output=True, text=True)
    try:
        assert r.returncode == 0, r.stderr
        assert json.loads((pushed / "auth.json").read_text()) == {"user": "panel", "password": HOSTILE}
        assert HOSTILE not in r.stdout + r.stderr
        assert not pathlib.Path("/tmp/scaffold-dev-ADD127--opt--control-panel-backend--config--auth.json").exists()
    finally:
        for path in files:
            pathlib.Path(path).unlink(missing_ok=True)


def test_no_secret_in_the_runner_stops_the_delivery(tmp_path):
    sh = {f["path"]: f["content"] for f in sr.file_writes(_rb())}["/tmp/scaffold-dev-ADD127--deliver.sh"]
    assert "the runner holds no PANEL_PASSWORD -- store it in Settings -> Machines" in sh


def test_service_state_alone_never_makes_a_developed_step_already_met():
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert "_state_only = _developed and all(" in src
    assert "if _depth < 6 and not _state_only:" in src
