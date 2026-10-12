r"""§17.1510 — a "configure/organize the Jellyfin library" step gives Jellyfin a
streaming-service layout: typed Movies (/media/movies) and TV Shows (/media/tv)
libraries, no leftover mixed library over /media, then a scan.

Live, job c75d77fa: the operator found Jellyfin "not organized". Measured, its three
libraries — Media (/media), Movies (/media/movies), Shows (/media/tv) — had NO
ContentType (all mixed) plus a redundant Media library over the root, so there was no
clean Movies/TV split or poster wall. The engine fixes it through Jellyfin's own
VirtualFolders API (remove+re-add each library typed, drop the mixed one, refresh).
Idempotent and reversible: library config only, never media files.
"""
from __future__ import annotations

import json
import subprocess

from app.modules.supervised_runs import rewrite_organize_jellyfin, _ORGANIZE_JELLYFIN_PY
from app.modules import machine_values as mv


class _Svc:
    def __init__(self, name, guest, ports):
        self.name, self.guest, self.ports = name, guest, ports


SERVICES = [_Svc("jellyfin", "101", ("8096",))]
INV = {"names": {"101": "jellyfin"}}
NODE = {"title": "Configure Jellyfin libraries",
        "prompt_template": "Organize the Jellyfin library into Movies and TV Shows like a streaming "
                           "service with posters and metadata."}

# the live state: three libraries, none typed, a redundant mixed one over /media.
CURRENT = [
    {"Name": "Media", "CollectionType": None, "Locations": ["/media"]},
    {"Name": "Movies", "CollectionType": None, "Locations": ["/media/movies"]},
    {"Name": "Shows", "CollectionType": None, "Locations": ["/media/tv"]},
]


def test_the_step_is_rewritten_to_the_jellyfin_api_in_the_guest():
    cmds, verify, notes = rewrite_organize_jellyfin(NODE, ["<model>"], ["chk"], SERVICES, INV)
    assert notes and "§17.1510" in notes[0]["why"]
    run = cmds[0]
    assert run.startswith("pct exec 101 -- sh -c '")
    assert "/Library/VirtualFolders" in run and "<JELLYFIN_API_KEY>" in run and "X-Emby-Token" in run


def test_the_verify_is_read_only_safe():
    from app.modules.assist_state_check import read_only_command
    _, verify, _ = rewrite_organize_jellyfin(NODE, ["x"], ["true"], SERVICES, INV)
    v = verify[0]
    assert v != "true" and "python3" not in v and "$(" not in v
    assert "/Library/VirtualFolders" in v and read_only_command(v)


def test_the_key_fills_and_the_command_parses():
    cmds, verify, _ = rewrite_organize_jellyfin(NODE, ["x"], ["y"], SERVICES, INV)
    c, v, _ = mv.read_on_the_machine(cmds, verify, INV)
    assert "<JELLYFIN_API_KEY>" not in c[0] and "jellyfin.db" in c[0]
    assert subprocess.run(["bash", "-n", "-c", c[0]], capture_output=True, text=True).returncode == 0
    assert subprocess.run(["bash", "-n", "-c", v[0]], capture_output=True, text=True).returncode == 0


def test_it_types_movies_and_shows_and_removes_the_mixed_library(monkeypatch):
    """Exec the script in-process with a stubbed Jellyfin at the live three-library state:
    it must type Movies (movies) and Shows (tvshows), remove the mixed Media library, and
    refresh — through add/delete on VirtualFolders, never touching media files."""
    import urllib.request as ur
    import io
    import contextlib
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append((req.get_method(), req.full_url))
        if req.get_method() == "GET" and req.full_url.endswith("/Library/VirtualFolders"):
            return io.BytesIO(json.dumps(CURRENT).encode())
        return io.BytesIO(b"")

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)
    monkeypatch.setattr("sys.argv", ["-c", "thekey"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(_ORGANIZE_JELLYFIN_PY, {"__name__": "__main__"})
    out = buf.getvalue()
    assert "SET: Movies -> movies (/media/movies)" in out
    assert "SET: Shows -> tvshows (/media/tv)" in out
    assert "REMOVED mixed library: Media" in out
    assert "scan triggered" in out
    # the mutations hit the right endpoints
    joined = " ".join(f"{m} {u}" for m, u in calls)
    assert "collectionType=movies" in joined and "collectionType=tvshows" in joined
    assert "POST http://127.0.0.1:8096/Library/Refresh" in joined
    assert "DELETE http://127.0.0.1:8096/Library/VirtualFolders?name=Media" in joined


def test_an_already_typed_library_is_left_alone(monkeypatch):
    """Idempotent: a library already typed and pointed right is not removed/re-added."""
    import urllib.request as ur
    import io
    import contextlib
    typed = [
        {"Name": "Movies", "CollectionType": "movies", "Locations": ["/media/movies"]},
        {"Name": "Shows", "CollectionType": "tvshows", "Locations": ["/media/tv"]},
    ]
    posts = []

    def fake_urlopen(req, timeout=None):
        if req.get_method() == "GET":
            return io.BytesIO(json.dumps(typed).encode())
        posts.append(req.full_url)
        return io.BytesIO(b"")

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)
    monkeypatch.setattr("sys.argv", ["-c", "k", "dry"])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        exec(_ORGANIZE_JELLYFIN_PY, {"__name__": "__main__"})
    out = buf.getvalue()
    assert "OK: Movies already movies" in out and "OK: Shows already tvshows" in out


def test_a_non_jellyfin_step_is_left_to_the_model():
    node = {"title": "Configure Radarr future monitoring", "prompt_template": "set up radarr quality profiles"}
    out, _, notes = rewrite_organize_jellyfin(node, ["x"], ["y"], SERVICES, INV)
    assert out == ["x"] and not notes


def test_frame_run_runs_the_organize_rewrite_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_organize_jellyfin(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_organize_jellyfin(node, cmds") < src.index("read_on_the_machine(cmds")
