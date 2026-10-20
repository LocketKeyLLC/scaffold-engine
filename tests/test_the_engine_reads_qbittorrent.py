"""§17.1708 — the engine reads qBittorrent itself: a login then a GET, inside its guest, the password via the environment."""
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

import pytest

from app.modules import qbittorrent_read as qr
from app.modules import supervised_runs as sr
from app.modules.service_truth import ServiceTruth

# live, job e5e22b92 T1 (2026-10-20): drafted five ways, refused or cut each time
T1 = {"node_key": "T1", "title": "Login qBittorrent list torrents", "tool": "Shell",
      "description": "Read-only: authenticate with stored credentials and list all torrents. No changes.",
      "prompt_template": "Read-only: authenticate with stored credentials and list all torrents. No changes."}
QBT = ServiceTruth(guest="105", unit="qbittorrent-nox.service", name="qbittorrent-nox", state="active",
                   ports=("61661", "8080"), data_dir="/var/lib/qbittorrent-nox", configs=())
DRAFT = ("## Run this\n\n```bash\npct exec 105 -- sh -c 'curl -s -b /tmp/c \"http://127.0.0.1:8080/api/v2/torrents/info\" | "
         "python3 -c \"import json,sys; print(len(json.load(sys.stdin)))\"'\n```\n")


def _frame(node):
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": ["MASS_PASSWORD"], "held_logins": [], "can_write_files": True}
    return sr.frame_run(node, DRAFT, spec, policy, services=[QBT],
                        inventory={"vms": {}, "cts": {"105": "running"}, "names": {"105": "qbittorrent"}})


def test_the_live_step_becomes_the_engines_read_with_no_refusal():
    f = _frame(T1)
    assert len(f["commands"]) == 1 and qr.is_engine_read(f["commands"][0]), f["commands"]
    assert f["commands"][0].startswith('pct exec 105 -- env QBT_PASS="$MASS_PASSWORD"')
    assert not f["refused"], [r["why"][:160] for r in f["refused"]]


def test_a_quoted_name_filters_and_a_change_step_is_not_claimed():
    pr = {**T1, "title": "Find the S3 pack in qBittorrent",
          "description": "Read-only: list qBittorrent torrents whose name has 'Mighty Morphin'."}
    assert qr.name_filter(pr) == ["mighty", "morphin"]
    assert qr.name_filter(T1) == []
    change = {**T1, "title": "Raise qBittorrent's active downloads", "description": "Set max_active_downloads to 10.",
              "prompt_template": "Set max_active_downloads to 10."}
    assert qr.block(change, lambda: "105") is None


class _Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n).decode()
        ok = "password=s3cret" in body and "username=admin" in body
        self.send_response(200)
        if ok:
            self.send_header("Set-Cookie", "SID=abc; path=/")
        self.end_headers()
        self.wfile.write(b"Ok." if ok else b"Fails.")

    def do_GET(self):
        if "SID=abc" not in (self.headers.get("Cookie") or ""):
            self.send_response(403); self.end_headers(); return
        ts = [{"name": "Power Rangers: Mighty Morphin' S03/Movie 1 (Legacy Collection)", "state": "metaDL", "progress": 0,
               "size": 0, "num_seeds": 0, "category": "tv-sonarr", "hash": "deadbeefcafe", "added_on": 2},
              {"name": "Scrubs (2001) S01", "state": "downloading", "progress": 0.5, "size": 6.1e9, "num_seeds": 40,
               "category": "tv-sonarr", "hash": "0123456789ab", "added_on": 1}]
        self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(ts).encode())


def _run(password, words):
    srv = HTTPServer(("127.0.0.1", 0), _Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    prog = qr.READ_PY.replace("127.0.0.1:" + qr._PORT, "127.0.0.1:%d" % srv.server_address[1])
    env = {**os.environ, "QBT_PASS": password, "QBT_FILTER": json.dumps(words)}
    try:
        return subprocess.run([sys.executable, "-"], input=prog, capture_output=True, text=True, env=env, timeout=30)
    finally:
        srv.shutdown()


def test_the_program_logs_in_and_summarises_without_printing_the_password():
    r = _run("s3cret", ["mighty", "morphin"])
    assert r.returncode == 0, r.stderr
    assert "TORRENTS: 2" in r.stdout and "MATCHING mighty morphin: 1" in r.stdout
    assert "Mighty Morphin' S03/Movie 1" in r.stdout and "metaDL" in r.stdout and "Scrubs" not in r.stdout.split("MATCHING")[1]
    assert "s3cret" not in r.stdout + r.stderr


def test_a_refused_login_is_a_failure():
    r = _run("wrong", [])
    assert r.returncode == 1 and "FAILED: qBittorrent refused the login" in r.stdout
