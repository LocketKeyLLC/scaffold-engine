"""§17.1712 — the engine's own Sonarr manual import: series by tvdbId, preview without seriesId, each file mapped by its
own episode number to the TARGET season, refused before importing on a wrong count, ONE ManualImport command."""
import json
import os
import subprocess
import sys
import tempfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock

from app.modules import arr_manual_import as ami
from app.modules import supervised_runs as sr
from app.modules.service_truth import ServiceTruth

A = "/media/downloads/Power.Rangers.Beast.Morphers.S26.Ep01-22.1080p.NICKAPP.WEB-DL.AAC2.0.H264-KRS"
B = "/media/downloads/Power.Rangers.Super.Megaforce.S01.1080p.BluRay.x264-GUACAMOLE[rartv]"
PI2 = {"node_key": "PI2", "title": "Import the Beast Morphers S26 and Super Megaforce S21 packs", "tool": "Shell",
       "description": (f"Change, operator-approved. Power Rangers, tvdbId 72553.\nFOLDER {A} -> SEASON 26 COUNT 22\n"
                       f"FOLDER {B} -> SEASON 21 COUNT 20 SKIP EXTENDED\nCopy mode; touch nothing else.")}
PI2["prompt_template"] = PI2["description"]


def test_the_step_states_its_mapping():
    p = ami.plan(PI2)
    assert p == {"tvdbId": 72553, "folders": [{"path": A, "season": 26, "count": 22},
                                              {"path": B, "season": 21, "count": 20, "skip": "EXTENDED"}]}
    assert ami.plan({"title": "Read the queue", "description": f"FOLDER {A} -> SEASON 26, tvdbId 72553"}) is None


class _Sonarr(BaseHTTPRequestHandler):
    posts: list = []
    previews: list = []

    def log_message(self, *a):
        pass

    def _send(self, obj):
        self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/v3/series":
            return self._send([{"id": 2, "title": "How I Met Your Mother", "tvdbId": 75760},
                               {"id": 6, "title": "Power Rangers", "tvdbId": 72553}])
        if u.path == "/api/v3/episode":
            s = int(q["seasonNumber"][0]); return self._send([{"id": s * 1000 + n, "episodeNumber": n} for n in range(1, 23)])
        if u.path == "/api/v3/manualimport":
            _Sonarr.previews.append(q)
            f = q["folder"][0]
            if f == A:
                return self._send([{"path": f"{f}/Power.Rangers.Beast.Morphers.S26E{n:02d}.1080p.mkv", "quality": {"q": 1},
                                    "languages": []} for n in range(1, 23)])
            return self._send([{"path": f"{f}/Power.Rangers.Super.Megaforce.S01E{n:02d}.1080p.BluRay.mkv", "quality": {"q": 2},
                                "languages": []} for n in range(1, 21)]
                              + [{"path": f"{f}/Power.Rangers.Super.Megaforce.S01E20.EXTENDED.1080p.BluRay.mkv",
                                  "quality": {"q": 2}, "languages": []}])
        if u.path.startswith("/api/v3/command/"):
            return self._send({"id": 7, "status": "completed", "message": "Completed"})
        if u.path == "/api/v3/series/6":
            return self._send({"seasons": [{"seasonNumber": 21, "statistics": {"episodeFileCount": 20, "episodeCount": 20}},
                                           {"seasonNumber": 26, "statistics": {"episodeFileCount": 22, "episodeCount": 22}}]})
        self.send_response(404); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _Sonarr.posts.append(json.loads(self.rfile.read(n))); self._send({"id": 7})


def _run(plan):
    _Sonarr.posts, _Sonarr.previews = [], []
    srv = HTTPServer(("127.0.0.1", 0), _Sonarr)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    prog = ami.IMPORT_PY.replace("127.0.0.1:" + ami._PORT, "127.0.0.1:%d" % srv.server_address[1])
    try:
        return subprocess.run([sys.executable, "-c", prog, "k", json.dumps(plan)], capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()


def test_the_program_maps_each_file_by_its_own_number_to_the_target_season_with_one_import():
    r = _run(ami.plan(PI2))
    assert r.returncode == 0, r.stdout + r.stderr
    assert len(_Sonarr.posts) == 1 and _Sonarr.posts[0]["name"] == "ManualImport" and _Sonarr.posts[0]["importMode"] == "copy"
    files = _Sonarr.posts[0]["files"]
    assert len(files) == 42 and all(f["seriesId"] == 6 for f in files)                 # Power Rangers, not the first show
    mm = {f["path"].rsplit("/", 1)[-1]: f["episodeIds"][0] for f in files}
    assert mm["Power.Rangers.Super.Megaforce.S01E07.1080p.BluRay.mkv"] == 21007        # S01E07 -> SEASON 21 episode 7
    assert mm["Power.Rangers.Beast.Morphers.S26E22.1080p.mkv"] == 26022
    assert not any("EXTENDED" in p for p in mm)
    assert all("seriesId" not in q for q in _Sonarr.previews)                          # the preview reads the FOLDER
    assert "MAP: 20 files of" in r.stdout and "S21E01..E20" in r.stdout and "SEASON 21: have 20 of 20" in r.stdout


def test_a_wrong_count_imports_nothing():
    p = ami.plan(PI2); p["folders"][1]["count"] = 21
    r = _run(p)
    assert r.returncode == 1 and "nothing imported" in r.stdout and _Sonarr.posts == []


def test_the_command_carries_the_program_key_and_plan_through_the_shell():
    cmd = ami.block(PI2, lambda: "104")[0][0]
    assert cmd.startswith("pct exec 104 -- sh -c '")
    with tempfile.TemporaryDirectory() as d:
        stub = os.path.join(d, "python3")
        out = os.path.join(d, "argv.json")
        with open(stub, "w") as f:
            f.write(f"#!{sys.executable}\nimport sys,json\njson.dump(sys.argv[1:], open({out!r},'w'))\n")
        os.chmod(stub, 0o755)
        inner = cmd.replace("pct exec 104 -- ", "", 1).replace("<SONARR_API_KEY>", "thekey")
        subprocess.run(["bash", "-c", inner], env={**os.environ, "PATH": d + ":" + os.environ["PATH"]}, check=True, timeout=20)
        argv = json.load(open(out))
    assert argv[0] == "-c" and argv[1] == ami.IMPORT_PY and argv[2] == "thekey" and json.loads(argv[3]) == ami.plan(PI2)


def test_the_live_step_is_claimed_with_no_refusal():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "24", "held": ["SONARR_API_KEY"], "held_logins": [], "can_write_files": True}
    son = ServiceTruth(guest="104", unit="sonarr.service", name="sonarr", state="active", ports=("8989",),
                       data_dir="/var/lib/sonarr", configs=("/var/lib/sonarr/config.xml",))
    f = sr.frame_run(PI2, "## Run this\n\n```bash\necho draft\n```\n", spec, policy, services=[son],
                     inventory={"vms": {}, "cts": {"104": "running"}, "names": {"104": "sonarr"}})
    assert len(f["commands"]) == 1 and ami.IMPORT_PY.replace("'", "'\\''") in f["commands"][0], f["commands"][:1]
    assert not f["refused"], [r["why"][:200] for r in f["refused"]]
