"""§17.1674 — a panel section passes only when nothing in it reports failing (live, job 5db1b088 T27: every section
PASSED on field names while the page showed Palworld as "error")."""
import os
import subprocess
from types import SimpleNamespace

from app.modules import api_route_check as arc

SVC = SimpleNamespace(guest="111", ports=("3001",), name="control-panel", unit="control-panel.service")
OVERVIEW_ERR = ('{"status":"ok","service":"control-panel","dependencies":{"radarr":{"name":"radarr","status":"ok",'
                '"host":"192.168.1.22","port":7878},"palworld":{"status":"error","error":"Command failed: ssh -o '
                'BatchMode=yes aedefruscio@192.168.1.44 sudo systemctl is-active palworld.service"}}}')
OVERVIEW_OK = '{"status":"ok","dependencies":{"palworld":{"name":"palworld","status":"ok","host":"192.168.1.44"}}}'


def _run(tmp_path, body):
    b = tmp_path / "bin"; b.mkdir(exist_ok=True)
    (tmp_path / "body").write_text(body)
    (b / "curl").write_text(f'#!/bin/sh\ncat {tmp_path}/body; printf " HTTPCODE200"\n'); (b / "curl").chmod(0o755)
    cmd = arc.sections_command(SVC, {"/api/overview": ["dependencies", "status"]}, ("PANEL_PASSWORD", "panel"))
    inner = subprocess.run(["bash", "-c", "eval \"set -- ${0#*sh -c }\"; printf '%s' \"$1\"",
                            cmd.split(" <<< ")[0]], capture_output=True, text=True).stdout
    return subprocess.run(["sh", "-c", inner], input="pw\n", capture_output=True, text=True,
                          env={**os.environ, "PATH": f"{b}:{os.environ['PATH']}"})


def test_an_error_inside_a_200_fails_the_section_and_says_what(tmp_path):
    r = _run(tmp_path, OVERVIEW_ERR)
    assert r.returncode == 1 and "FAIL /api/overview HTTP 200, the page shows these as failing" in r.stdout, r
    assert '"palworld"' in r.stdout and "Command failed" in r.stdout and '"radarr"' not in r.stdout


def test_a_healthy_answer_passes(tmp_path):
    r = _run(tmp_path, OVERVIEW_OK)
    assert r.returncode == 0 and "PASS /api/overview (2 fields, nothing reported failing)" in r.stdout, r


def test_a_missing_field_still_fails(tmp_path):
    r = _run(tmp_path, '{"status":"ok"}')
    assert r.returncode == 1 and "lacks: dependencies" in r.stdout


def test_no_temp_file_and_it_parses():
    cmd = arc.sections_command(SVC, {"/api/overview": ["dependencies"]}, ("PANEL_PASSWORD", "panel"))
    assert "/tmp/" not in cmd
    assert subprocess.run(["bash", "-n", "-c", cmd], capture_output=True).returncode == 0


def test_a_stopped_server_with_a_nested_answer_fails_too(tmp_path):
    """the panel's not-active shape: the error entry carries a nested `response` object"""
    body = ('{"status":"ok","dependencies":{"palworld":{"name":"palworld","status":"error","host":"192.168.1.44",'
            '"port":null,"response":{"status":"failed"}},"radarr":{"name":"radarr","status":"ok","response":{"v":1}}}}')
    r = _run(tmp_path, body)
    assert r.returncode == 1 and '"palworld"' in r.stdout and '"radarr"' not in r.stdout, r
    ok = '{"status":"ok","dependencies":{"palworld":{"name":"palworld","status":"ok","response":{"status":"active"}}}}'
    assert _run(tmp_path, ok).returncode == 0


def test_a_read_only_step_takes_the_engines_own_section_check():
    """§17.1678 — live, PW1 (declared read only) refused the engine's own section check as "not a read\""""
    from app.modules import supervised_runs as sr
    node = {"node_key": "PW1", "title": "Verify control panel Palworld boxes as the page sees them",
            "prompt_template": "Read only, change nothing."}
    rb = arc.sections_runbook(SVC, {"/api/overview": ["dependencies"]}, ("PANEL_PASSWORD", "panel"))
    cmd = arc.sections_command(SVC, {"/api/overview": ["dependencies"]}, ("PANEL_PASSWORD", "panel"))
    assert sr.writes_in_a_read_only_step([cmd], node, rb) == []
    assert sr.writes_in_a_read_only_step(["apt-get install -y ipmitool"], node, "## Run this\n")   # a real write still is
