"""§17.1415 — a developed feature's API key is filled in ON THE MACHINE at delivery; and the API
read-back gate judges what the BLOCK does, not the code it delivers.

Live, 2026-10-07, ADD123 (the media-request capability): the developed route wrote
`config/media-request.json` with `"radarrApiKey": ""` -- the model cannot know the key, and by design
(§17.1391) a credential never travels through the engine. The engine CAN read Radarr's key where
Radarr keeps it (§17.1332), so the model writes a marker and the delivery fills it on the host.
And §17.1362's gate refused the frame for "changing an API no check reads back" -- counting the
POST the acceptance step makes WITH `curl -f` (which cannot exit 0 on a 400), and the HTTP calls in
the delivered route's own source (which a user triggers later, not the block).
"""
from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import tempfile

import pytest

from app.modules import develop as dv
from app.modules import service_truth as st
from app.modules import supervised_runs as sr

FIX = pathlib.Path(__file__).parent / "fixtures"
ADD123 = json.loads((FIX / "add123_node_2026_10_07.json").read_text())
RADARR = st.ServiceTruth(guest="103", name="radarr", unit="radarr.service")
SONARR = st.ServiceTruth(guest="104", name="sonarr", unit="sonarr.service")
PANEL = st.ServiceTruth(guest="111", name="control-panel", unit="control-panel.service", workdir="/opt/control-panel-backend")
HOST = dv.Host(guest="111", vm=False, workdir="/opt/control-panel-backend", unit="control-panel.service")
MARK = dv.SECRET_MARK.format(name="RADARR_API_KEY")
CFG = {"/opt/control-panel-backend/config/media-request.json": '{"radarrApiKey": "' + MARK + '"}\n'}


# ── which keys, and how the model is told ───────────────────────────────────

def test_the_engine_offers_the_keys_it_can_read_on_the_machine():
    creds = dv.credentials_for([RADARR, SONARR, PANEL])
    assert sorted(creds) == ["RADARR_API_KEY", "SONARR_API_KEY"]
    assert creds["RADARR_API_KEY"].startswith("pct exec 103 -- sh -c 'cat /var/lib/radarr/config.xml")
    doc = dv.credentials_doc(creds, [RADARR, SONARR])
    assert MARK in doc and "never passes through you" in doc and "guest 103" in doc


def test_a_service_the_registry_does_not_know_offers_nothing():
    assert dv.credentials_for([PANEL, st.ServiceTruth(guest="130", name="pihole")]) == {}


# ── the delivery fills it on the host and the frame never holds it ──────────

def _delivery(creds=None):
    return dv.render_delivery(ADD123, HOST, CFG, ["true"], [], creds if creds is not None else dv.credentials_for([RADARR]))


def test_the_frame_shows_the_marker_never_a_key():
    rb = _delivery()
    files = {f["path"]: f["content"] for f in sr.file_writes(rb)}
    staged = "/tmp/scaffold-dev-ADD123--opt--control-panel-backend--config--media-request.json"
    assert MARK in files[staged]
    sh = files["/tmp/scaffold-dev-ADD123--deliver.sh"]
    assert "V_RADARR_API_KEY=$(pct exec 103 -- sh -c 'cat /var/lib/radarr/config.xml" in sh
    assert 'could not read RADARR_API_KEY on the machine' in sh
    assert f'sed -i "s|{MARK}|$V_RADARR_API_KEY|g" {staged}' in sh
    assert sh.index("trap ") < sh.index("V_RADARR_API_KEY=")
    assert subprocess.run(["bash", "-n"], input=sh, text=True).returncode == 0


def test_a_marker_the_engine_cannot_read_stops_the_delivery():
    sh = {f["path"]: f["content"] for f in sr.file_writes(_delivery({}))}["/tmp/scaffold-dev-ADD123--deliver.sh"]
    assert "the engine cannot read RADARR_API_KEY on any machine" in sh and "exit 1" in sh


def test_bash_level_the_key_lands_in_the_pushed_file_and_the_staged_copy_is_gone(tmp_path):
    """Run the REAL rendered deliver.sh with a stub `pct` that answers Radarr's config.xml."""
    rb = _delivery()
    files = {f["path"]: f["content"] for f in sr.file_writes(rb)}
    pushed = tmp_path / "pushed"
    pushed.mkdir()
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "pct").write_text(
        "#!/bin/bash\n"
        'case "$1" in\n'
        '  status) echo "status: running";;\n'
        '  push) cp "$3" "' + str(pushed) + '/$(basename "$4")";;\n'
        '  exec) shift 2; [ "$1" = "--" ] && shift;\n'
        '        if [[ "$*" == *config.xml* ]]; then echo "<Config><ApiKey>c0ffee1234567890abcdef1234567890</ApiKey></Config>";\n'
        '        elif [ "$1" = "systemctl" ] && [ "$2" = "is-active" ]; then echo active; fi;;\n'
        "esac\n")
    (stub / "pct").chmod(0o755)
    for path, content in files.items():                      # the runner writes the staged files first
        pathlib.Path(path).write_text(content)
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}
    r = subprocess.run(["bash", "/tmp/scaffold-dev-ADD123--deliver.sh"], env=env, capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert json.loads((pushed / "media-request.json").read_text()) == {"radarrApiKey": "c0ffee1234567890abcdef1234567890"}
    assert "c0ffee" not in r.stdout + r.stderr, "the key is never printed"
    assert not pathlib.Path("/tmp/scaffold-dev-ADD123--opt--control-panel-backend--config--media-request.json").exists()
    for path in files:
        pathlib.Path(path).unlink(missing_ok=True)


# ── the read-back gate judges what the block runs ────────────────────────────

ROUTE = "http.request({hostname: '192.168.1.22', port: 7878, path: '/api/v3/movie', method: 'POST'})"


def test_the_delivered_routes_own_api_calls_are_not_the_blocks():
    files = [{"path": "/tmp/scaffold-dev-ADD123--opt--x--route.js", "content": ROUTE}]
    assert sr.changes_an_api_without_reading_it(["bash /tmp/scaffold-dev-ADD123--deliver.sh"], [], files) == []


def test_the_steps_own_acceptance_check_is_its_check():
    acc = dv.acceptance_checks(ADD123)
    assert sr.changes_an_api_without_reading_it(acc, [], [], acc) == []


def test_fail_with_body_is_not_an_exemption():
    """Full suite caught it: exempting any `--fail` let ADD132's `curl --fail-with-body -X PUT` through.
    Failing on a 400 proves nothing about the value the PUT left; only identity with the step's own
    acceptance check exempts."""
    put = "curl -s --fail-with-body -X PUT -d '{}' http://192.168.1.22:7878/api/v3/downloadclient/1"
    assert sr.changes_an_api_without_reading_it([put], [], [])
    acc = "pct exec 111 -- curl -f -s -X POST -d '{}' http://127.0.0.1:3001/api/media-request"
    assert sr.changes_an_api_without_reading_it([acc], [], [])          # not the step's check → still judged


def test_frame_run_passes_the_acceptance_checks_to_the_gate():
    import inspect
    assert "_develop.acceptance_checks(node)" in inspect.getsource(sr.frame_run)


def test_a_plain_curl_post_is_still_refused():
    """Vacuity guard: the gate still bites on what it was built for."""
    plain = "curl -s -X PUT -d '{}' http://192.168.1.22:7878/api/v3/downloadclient/1"
    assert sr.changes_an_api_without_reading_it([plain], [], [])


def test_a_script_the_block_runs_is_still_judged():
    files = [{"path": "/tmp/fix.sh", "content": "curl -s -X PUT -d '{}' http://192.168.1.22:7878/api/v3/x"}]
    assert sr.changes_an_api_without_reading_it(["bash /tmp/fix.sh"], [], files)
