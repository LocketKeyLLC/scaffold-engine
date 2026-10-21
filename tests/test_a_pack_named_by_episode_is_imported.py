"""§17.1722 — the manual import takes a quoted folder with spaces, and files that name only the episode.

Live (job e5e22b92 SI1, 2026-10-21): the Power Rangers S25 pack finished as
`/media/downloads/Power Rangers Super Ninja Steel (Season 25, 2018)` holding `Episode 1 Echoes of Evil.mkv` …
`Episode 22 The Poisy Show.mkv` (E18 an `.mp4`); Sonarr's queue said "Invalid season or episode" for each and its
preview "Unknown Series". §17.1712's block read `FOLDER /\\S+` (the path up to its first space) and numbered a file
only by `SxxEyy`, so the step that imports it would have been refused.
"""
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

C = "/media/downloads/Power Rangers Super Ninja Steel (Season 25, 2018)"
# live, SI1's file names (the ones that matter: two-digit, an .mp4, a hyphen and an "Ep" word in the title)
NAMES = {1: "Echoes of Evil.mkv", 6: "Attack of the Galactic Ninjas.mkv", 7: "The Need for Speed.mkv",
         18: "Monster Mix-Up.mp4", 22: "The Poisy Show.mkv"}
SI2 = {"node_key": "SI2", "title": "Import the Power Rangers S25 pack", "tool": "Shell",
       "description": f'Change, operator-approved. Power Rangers, tvdbId 72553.\nFOLDER "{C}" -> SEASON 25 COUNT 22\n'
                      "Copy mode; touch nothing else."}
SI2["prompt_template"] = SI2["description"]


def test_the_quoted_folder_is_read_whole_and_the_bare_one_as_before():
    assert ami.plan(SI2) == {"tvdbId": 72553, "folders": [{"path": C, "season": 25, "count": 22}]}
    text = "Import. tvdbId 72553\nFOLDER /media/downloads/A.S26.x -> SEASON 26 COUNT 22."
    bare = dict(SI2, description=text, prompt_template=text)
    assert ami.plan(bare)["folders"] == [{"path": "/media/downloads/A.S26.x", "season": 26, "count": 22}]


class _Sonarr(BaseHTTPRequestHandler):
    posts: list = []
    names: dict = {}

    def log_message(self, *a):
        pass

    def _send(self, obj):
        self.send_response(200); self.end_headers(); self.wfile.write(json.dumps(obj).encode())

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if u.path == "/api/v3/series":
            return self._send([{"id": 6, "title": "Power Rangers", "tvdbId": 72553}])
        if u.path == "/api/v3/episode":
            return self._send([{"id": 25000 + n, "episodeNumber": n} for n in range(1, 23)])
        if u.path == "/api/v3/manualimport":
            assert q["folder"][0] == C and "seriesId" not in q
            return self._send([{"path": f"{C}/{nm}", "quality": {"q": 1}, "languages": []} for nm in _Sonarr.names.values()])
        if u.path.startswith("/api/v3/command/"):
            return self._send({"id": 9, "status": "completed", "message": "Completed"})
        if u.path == "/api/v3/series/6":
            return self._send({"seasons": [{"seasonNumber": 25, "statistics": {"episodeFileCount": 22, "episodeCount": 22}}]})
        self.send_response(404); self.end_headers()

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        _Sonarr.posts.append(json.loads(self.rfile.read(n))); self._send({"id": 9})


def _run(names, plan):
    _Sonarr.posts, _Sonarr.names = [], names
    srv = HTTPServer(("127.0.0.1", 0), _Sonarr)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    prog = ami.IMPORT_PY.replace("127.0.0.1:" + ami._PORT, "127.0.0.1:%d" % srv.server_address[1])
    try:
        return subprocess.run([sys.executable, "-c", prog, "k", json.dumps(plan)], capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()


def _all22():
    return {n: f"Episode {n} {NAMES.get(n, f'Title {n}.mkv')}" for n in range(1, 23)}


def test_the_live_pack_maps_episode_n_to_s25en_with_one_import():
    r = _run(_all22(), ami.plan(SI2))
    assert r.returncode == 0, r.stdout + r.stderr
    files = _Sonarr.posts[0]["files"]
    mm = {f["path"].rsplit("/", 1)[-1]: f["episodeIds"] for f in files}
    assert len(files) == 22 and mm["Episode 18 Monster Mix-Up.mp4"] == [25018] and mm["Episode 7 The Need for Speed.mkv"] == [25007]
    assert "S25E01..E22" in r.stdout and "SEASON 25: have 22 of 22" in r.stdout


def test_a_name_with_no_number_or_a_duplicate_imports_nothing():
    names = _all22(); names[5] = "Bonus Featurette.mkv"
    r = _run(names, ami.plan(SI2))
    assert r.returncode == 1 and "maps to no episode" in r.stdout and _Sonarr.posts == []
    names = _all22(); names[5] = "Episode 6 Again.mkv"
    r = _run(names, ami.plan(SI2))
    assert r.returncode == 1 and "map to one episode" in r.stdout and _Sonarr.posts == []


def test_an_sxxeyy_name_still_wins_over_an_episode_word():
    names = {n: f"Power.Rangers.S25E{n:02d}.Episode.{30 + n}.mkv" for n in range(1, 23)}
    r = _run(names, ami.plan(SI2))
    assert r.returncode == 0 and "S25E01..E22" in r.stdout


def test_the_command_carries_the_spaced_path_through_the_shell():
    cmd = ami.block(SI2, lambda: "104")[0][0]
    with tempfile.TemporaryDirectory() as d:
        stub, out = os.path.join(d, "python3"), os.path.join(d, "argv.json")
        with open(stub, "w") as f:
            f.write(f"#!{sys.executable}\nimport sys,json\njson.dump(sys.argv[1:], open({out!r},'w'))\n")
        os.chmod(stub, 0o755)
        inner = cmd.replace("pct exec 104 -- ", "", 1).replace("<SONARR_API_KEY>", "thekey")
        subprocess.run(["bash", "-c", inner], env={**os.environ, "PATH": d + ":" + os.environ["PATH"]}, check=True, timeout=20)
        argv = json.load(open(out))
    assert json.loads(argv[3])["folders"][0]["path"] == C


def test_the_step_is_claimed_with_no_refusal():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "24", "held": ["SONARR_API_KEY"], "held_logins": [], "can_write_files": True}
    son = ServiceTruth(guest="104", unit="sonarr.service", name="sonarr", state="active", ports=("8989",),
                       data_dir="/var/lib/sonarr", configs=("/var/lib/sonarr/config.xml",))
    f = sr.frame_run(SI2, "## Run this\n\n```bash\necho draft\n```\n", spec, policy, services=[son],
                     inventory={"vms": {}, "cts": {"104": "running"}, "names": {"104": "sonarr"}})
    assert len(f["commands"]) == 1 and not f["refused"], [r["why"][:200] for r in f["refused"]]
