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


def test_a_step_that_only_mentions_qbittorrent_is_not_claimed_and_an_apostrophe_is_not_a_quote():
    """§17.1711 — live, job e5e22b92 PI1 (a Sonarr manual-import preview) was claimed with filter 's container (ct 104)…'"""
    pi1 = {"node_key": "PI1", "title": "Preview a manual import of the two finished Power Rangers packs",
           "description": ("Read-only, change nothing. Inside Sonarr's container (CT 104): find Sonarr's download folder from its "
                           "qBittorrent download client settings, and list the entries whose names contain 'Beast Morphers S26 "
                           "COMPLETE' or 'Super.Megaforce.S01'.")}
    assert not qr.wants(pi1)
    assert qr.name_filter(pi1) == ["beast", "morphers", "s26", "complete"]
    for claimed in ({"title": "List the Power Rangers torrents in qBittorrent", "description": "Read-only, change nothing."},
                    {"title": "Check the downloads", "description": "Read-only: list qBittorrent torrents whose name has 'Scrubs'."}):
        assert qr.wants(claimed), claimed


class _FakeStatus(_Fake):
    def do_GET(self):
        if "SID=abc" not in (self.headers.get("Cookie") or ""):
            self.send_response(403); self.end_headers(); return
        body = {"/api/v2/app/version": b"v4.6.4",
                "/api/v2/transfer/info": json.dumps({"connection_status": "firewalled", "dht_nodes": 0,
                                                     "dl_info_speed": 2048, "up_info_speed": 0}).encode(),
                "/api/v2/app/preferences": json.dumps({"listen_port": 61661, "upnp": False, "random_port": False,
                                                       "current_network_interface": "wg0", "web_ui_password": "NOPE",
                                                       "proxy_password": "NOPE2", "max_active_downloads": 3}).encode()}.get(self.path)
        self.send_response(200 if body else 404); self.end_headers(); self.wfile.write(body or b"")


def test_the_connection_read_reports_status_and_never_a_password_field():
    """§17.1714 — connected/firewalled, DHT, port, interface; the preference whitelist never prints a password field"""
    srv = HTTPServer(("127.0.0.1", 0), _FakeStatus)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    prog = qr.STATUS_PY.replace("127.0.0.1:" + qr._PORT, "127.0.0.1:%d" % srv.server_address[1])
    try:
        r = subprocess.run([sys.executable, "-"], input=prog, capture_output=True, text=True, timeout=30,
                           env={**os.environ, "QBT_PASS": "s3cret"})
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stderr
    assert "CONNECTION: firewalled | DHT nodes 0" in r.stdout and "PREF listen_port = 61661" in r.stdout
    assert "PREF current_network_interface = wg0" in r.stdout and "PREF max_active_downloads = 3" in r.stdout
    assert "NOPE" not in r.stdout and "s3cret" not in r.stdout


def test_a_connection_step_gets_the_status_read_with_no_refusal():
    qs = {"node_key": "QS1", "title": "Check qBittorrent's connection status", "tool": "Shell",
          "description": "Read-only, change nothing. Read qBittorrent's connection status: connected or firewalled, DHT nodes, "
                         "listening port and interface, compared with the VPN's forwarded port."}
    qs["prompt_template"] = qs["description"]
    assert qr.wants_status(qs) and not qr.wants_status(T1)
    f = _frame(qs)
    assert qr.is_engine_read(f["commands"][0]) and qr.STATUS_PY in f["commands"][0]
    assert f["commands"][1:] == ["pct exec 105 -- ss -tlnu", "pct exec 105 -- ip -4 -brief addr"]
    assert not f["refused"], [r["why"][:160] for r in f["refused"]]
