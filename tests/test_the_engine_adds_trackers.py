"""§17.1715 — the engine adds the public tracker list to qBittorrent: new downloads and the stuck torrents."""
import json
import os
import subprocess
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

from app.modules import qbittorrent_trackers as qt
from app.modules import supervised_runs as sr
from app.modules.service_truth import ServiceTruth

TR1 = {"node_key": "TR1", "title": "Add the public tracker list to qBittorrent", "tool": "Shell",
       "description": ("Change, operator-approved. Add the public tracker list to qBittorrent for new downloads (automatically "
                       "add trackers) and to the stuck torrents (metaDL and stalledDL), then reannounce them.")}
TR1["prompt_template"] = TR1["description"]
LIST = "udp://a.example:1337/announce\n\nudp://b.example:6969/announce\nhttp://c.example:80/announce\n" \
       "udp://d.example:1337/announce\nhttps://e.example:443/announce\nudp://f.example:451/announce\n"


class _Qbt(BaseHTTPRequestHandler):
    calls: list = []
    trackers: dict = {}
    prefs: dict = {}

    def log_message(self, *a):
        pass

    def _ok(self, body):
        self.send_response(200); self.send_header("Set-Cookie", "SID=x; path=/"); self.end_headers()
        self.wfile.write(body if isinstance(body, bytes) else json.dumps(body).encode())

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if u.path == "/list.txt":
            return self._ok(_Qbt.list_body.encode())
        if "SID=x" not in (self.headers.get("Cookie") or ""):
            self.send_response(403); self.end_headers(); return
        _Qbt.calls.append(("GET", u.path))
        if u.path == "/api/v2/app/preferences":
            return self._ok(_Qbt.prefs)
        if u.path == "/api/v2/torrents/info":
            return self._ok([{"hash": "h1", "name": "PR S24E03", "state": "metaDL"},
                             {"hash": "h2", "name": "RC special", "state": "stalledDL"},
                             {"hash": "h3", "name": "Scrubs S01", "state": "stalledUP"}])
        if u.path == "/api/v2/torrents/trackers":
            return self._ok([{"url": x} for x in _Qbt.trackers.get(q["hash"][0], [])])
        self.send_response(404); self.end_headers()

    def do_POST(self):
        body = urllib.parse.parse_qs(self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode())
        p = urllib.parse.urlparse(self.path).path
        if p == "/api/v2/auth/login":
            ok = body.get("password") == ["s3cret"]
            self.send_response(200)
            if ok:
                self.send_header("Set-Cookie", "SID=x; path=/")
            self.end_headers(); self.wfile.write(b"Ok." if ok else b"Fails."); return
        if "SID=x" not in (self.headers.get("Cookie") or ""):
            self.send_response(403); self.end_headers(); return
        _Qbt.calls.append(("POST", p, body))
        if p == "/api/v2/app/setPreferences":
            _Qbt.prefs.update(json.loads(body["json"][0]))
        if p == "/api/v2/torrents/addTrackers":
            for h in body["hashes"][0].split("|"):
                _Qbt.trackers[h] = body["urls"][0].split("\n")
        self._ok(b"")


def _run(list_body, password="s3cret", modes="new,stuck"):
    _Qbt.calls, _Qbt.trackers, _Qbt.prefs, _Qbt.list_body = [], {}, {}, list_body
    srv = HTTPServer(("127.0.0.1", 0), _Qbt)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]
    prog = qt.TRACKERS_PY.replace("127.0.0.1:" + qt._PORT, "127.0.0.1:%d" % port).replace("time.sleep(8)", "time.sleep(0)").replace("time.sleep(3)", "time.sleep(0)")
    env = {**os.environ, "QBT_PASS": password, "QBT_MODES": modes, "QBT_LIST": "http://127.0.0.1:%d/list.txt" % port}
    try:
        return subprocess.run([sys.executable, "-"], input=prog, capture_output=True, text=True, env=env, timeout=60)
    finally:
        srv.shutdown()


def test_the_live_request_sets_new_downloads_and_fixes_only_the_stuck_torrents():
    assert qt.modes(TR1) == ["new", "stuck"]
    r = _run(LIST)
    assert r.returncode == 0, r.stdout + r.stderr
    assert _Qbt.prefs["add_trackers_enabled"] is True and len(_Qbt.prefs["add_trackers"].split("\n")) == 6
    adds = [c for c in _Qbt.calls if c[0] == "POST" and c[1] == "/api/v2/torrents/addTrackers"]
    assert len(adds) == 1 and adds[0][2]["hashes"] == ["h1|h2"]                 # the seeding Scrubs pack is left alone
    assert any(c[0] == "POST" and c[1] == "/api/v2/torrents/reannounce" for c in _Qbt.calls)
    assert "NEW DOWNLOADS: add_trackers_enabled=True, 6 trackers set" in r.stdout and "STUCK: 2 torrents" in r.stdout
    assert "s3cret" not in r.stdout + r.stderr


def test_a_short_or_missing_list_changes_nothing():
    r = _run("udp://only.one:1/announce\nnot a tracker\n")
    assert r.returncode == 1 and "nothing changed" in r.stdout and not [c for c in _Qbt.calls if c[0] == "POST"]


def test_the_step_is_claimed_with_its_check_and_no_refusal_and_others_are_not():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": ["MASS_PASSWORD"], "held_logins": [], "can_write_files": True}
    qbt = ServiceTruth(guest="105", unit="qbittorrent-nox.service", name="qbittorrent-nox", state="active",
                       ports=("61661", "8080"), data_dir="/var/lib/qbittorrent-nox", configs=())
    f = sr.frame_run(TR1, "## Run this\n\n```bash\necho draft\n```\n", spec, policy, services=[qbt],
                     inventory={"vms": {}, "cts": {"105": "running"}, "names": {"105": "qbittorrent"}})
    assert len(f["commands"]) == 1 and qt.TRACKERS_PY in f["commands"][0] and "QBT_MODES=new,stuck" in f["commands"][0]
    assert f["verify"] == [f"pct exec 105 -- grep -c -i 'AddTrackersEnabled=true' {qt.CONF}"]
    assert not f["refused"], [r["why"][:200] for r in f["refused"]]
    assert qt.modes({"title": "List the Power Rangers torrents in qBittorrent", "description": "Read-only."}) == []
