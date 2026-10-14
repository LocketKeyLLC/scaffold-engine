r"""§17.1562 — a Jellyfin library whose items resolved as the WRONG kind is rebuilt: remove, scan, re-add.

Live (job 263e920a, 2026-10-14): the census found Shows typed tvshows with a top level of `Folder x6` and
902 `Movie` items — rows left over from the mixed-content library that had covered /media. Jellyfin reuses
an existing item at a path and never re-types it. The model's drafts for the fix step did not converge
(a `systemctl stop` + `sqlite3` script on a guest with no sqlite3; then exploratory finds). Remove-and-re-add
is the fix others use; the engine owns the shape. `DELETE /Items/{id}` is NEVER used: it deletes media files.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import time

from app.modules.supervised_runs import (rewrite_rebuild_jellyfin_library, rewrite_organize_jellyfin,
                                         rewrite_refresh_jellyfin_metadata, rewrite_jellyfin_metadata_report,
                                         _REBUILD_JELLYFIN_PY, loops_the_network_without_a_budget,
                                         loop_dies_on_one_dead_party, checks_can_witness_the_change)
from app.modules import machine_values as mv


class _Svc:
    def __init__(self, name, guest, ports):
        self.name, self.guest, self.ports = name, guest, ports


SERVICES = [_Svc("jellyfin", "101", ("8096",))]
INV = {"names": {"101": "jellyfin"}}
FIX1 = {"node_key": "FIX1", "title": "Fix the Jellyfin TV library so its files resolve as Series, Seasons and Episodes",
        "prompt_template": "Measured on container 101: the library Shows is typed tvshows, yet Jellyfin resolved its "
                           "content as 6 Folder items and 902 Movie items. Make the Shows library re-resolve its "
                           "content as Series/Season/Episode. Do not touch media files.", "tool": "Shell"}
LIBS = [{"Name": "Movies", "CollectionType": "movies", "ItemId": "aaa", "Locations": ["/media/movies"]},
        {"Name": "Shows", "CollectionType": "tvshows", "ItemId": "bbb", "Locations": ["/media/tv"]}]
TOP = {"aaa": [{"Id": "m1", "Type": "Movie", "Name": "Sintel"}],
       "bbb": [{"Id": "f%d" % i, "Type": "Folder", "Name": "Show %d" % i} for i in range(6)]}


def test_the_fix_step_is_rewritten_to_the_rebuild_in_the_guest():
    cmds, verify, notes = rewrite_rebuild_jellyfin_library(FIX1, ["<model>"], ["chk"], SERVICES, INV)
    assert notes and "§17.1562" in notes[0]["why"] and "never touched" in notes[0]["why"]
    assert len(cmds) == 1 and cmds[0].startswith("pct exec 101 -- sh -c '") and "<JELLYFIN_API_KEY>" in cmds[0]
    assert "/Library/VirtualFolders" in cmds[0] and "/Items/%s" not in cmds[0] and "DELETE /Items" not in cmds[0]


def test_the_verify_is_the_type_markers_read_only_and_the_key_fills():
    from app.modules.assist_state_check import read_only_command
    cmds, verify, _ = rewrite_rebuild_jellyfin_library(FIX1, ["x"], ["y"], SERVICES, INV)
    assert ".collection" in verify[0] and "JELLYFIN_API_KEY" not in verify[0] and read_only_command(verify[0])
    c, v, _ = mv.read_on_the_machine(cmds, verify, INV)
    assert "<JELLYFIN_API_KEY>" not in c[0] and "jellyfin.db" in c[0] and not re.search(r"<[A-Z][A-Z0-9_]+>", c[0])
    assert subprocess.run(["bash", "-n", "-c", c[0]], capture_output=True, text=True).returncode == 0
    assert loops_the_network_without_a_budget(cmds) == [] and loop_dies_on_one_dead_party(cmds) == []
    assert "sends a change" in checks_can_witness_the_change(cmds, verify)      # §17.1559: a trigger, never pre-passed


def _run(monkeypatch, argv, libs=LIBS, top=TOP, task_states=("Running", "Idle")):
    import urllib.request as ur
    calls = []
    polls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        m, u = req.get_method(), req.full_url
        calls.append((m, u))
        if u.endswith("/Library/VirtualFolders") and m == "GET":
            return io.BytesIO(json.dumps(libs).encode())
        if "/Items?ParentId=" in u:
            lid = re.search(r"ParentId=([^&]+)", u).group(1)
            return io.BytesIO(json.dumps({"Items": top.get(lid, [])}).encode())
        if u.endswith("/ScheduledTasks"):
            st = task_states[min(polls["n"], len(task_states) - 1)]; polls["n"] += 1
            return io.BytesIO(json.dumps([{"Key": "RefreshLibrary", "State": st}]).encode())
        return io.BytesIO(b"")

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr("sys.argv", ["-c"] + argv)
    buf = io.StringIO(); code = 0
    with contextlib.redirect_stdout(buf):
        try:
            exec(_REBUILD_JELLYFIN_PY, {"__name__": "__main__"})
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return buf.getvalue(), calls, code, slept


def test_it_removes_waits_and_re_adds_only_the_mistyped_library_and_never_deletes_items(monkeypatch):
    out, calls, code, slept = _run(monkeypatch, ["k"])
    assert code == 0
    assert "WRONG TYPES: Shows (tvshows) top level is Folder, expected Series" in out
    assert "OK: Movies (movies) top level is Movie" in out
    assert "REMOVED library Shows" in out and "RE-ADDED library Shows as tvshows over /media/tv" in out
    assert "RESULT: 1 library rebuilt" in out
    joined = [f"{m} {u}" for m, u in calls]
    assert "DELETE http://127.0.0.1:8096/Library/VirtualFolders?name=Shows&refreshLibrary=true" in joined
    assert any(u.startswith("POST http://127.0.0.1:8096/Library/VirtualFolders?name=Shows&collectionType=tvshows&paths=/media/tv") for u in joined)
    assert not any("Movies" in u and m in ("DELETE", "POST") for m, u in calls)      # the right library is left alone
    assert not any("/Items/" in u for m, u in calls if m == "DELETE")                 # never the file-deleting endpoint
    # the removal's scan is waited for before the re-add (poll Running → Idle), then the re-add's scan
    assert slept == [10] or slept == [10, 10]
    i_del = joined.index("DELETE http://127.0.0.1:8096/Library/VirtualFolders?name=Shows&refreshLibrary=true")
    i_add = next(i for i, c in enumerate(joined) if c.startswith("POST http://127.0.0.1:8096/Library/VirtualFolders?name=Shows"))
    assert i_del < i_add and any("ScheduledTasks" in c for c in joined[i_del:i_add])


def test_a_library_that_resolves_right_is_left_alone(monkeypatch):
    good = {"aaa": TOP["aaa"], "bbb": [{"Id": "s1", "Type": "Series", "Name": "Scrubs"}]}
    out, calls, code, _ = _run(monkeypatch, ["k"], top=good)
    assert code == 0 and "nothing to rebuild" in out
    assert all(m == "GET" for m, _ in calls)


def test_dry_run_changes_nothing(monkeypatch):
    out, calls, code, _ = _run(monkeypatch, ["k", "dry"])
    assert code == 0 and "WOULD REBUILD: Shows -> remove, scan, re-add as tvshows over /media/tv" in out
    assert all(m == "GET" for m, _ in calls)


def test_a_failed_re_add_fails_the_step_and_says_how_to_restore(monkeypatch):
    import urllib.request as ur
    import urllib.error

    def fake_urlopen(req, timeout=None):
        m, u = req.get_method(), req.full_url
        if u.endswith("/Library/VirtualFolders") and m == "GET":
            return io.BytesIO(json.dumps(LIBS).encode())
        if "/Items?ParentId=" in u:
            lid = re.search(r"ParentId=([^&]+)", u).group(1)
            return io.BytesIO(json.dumps({"Items": TOP.get(lid, [])}).encode())
        if u.endswith("/ScheduledTasks"):
            return io.BytesIO(json.dumps([{"Key": "RefreshLibrary", "State": "Idle"}]).encode())
        if m == "POST":
            raise urllib.error.HTTPError(u, 500, "boom", {}, None)
        return io.BytesIO(b"")

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr("sys.argv", ["-c", "k"])
    buf = io.StringIO(); code = 0
    with contextlib.redirect_stdout(buf):
        try:
            exec(_REBUILD_JELLYFIN_PY, {"__name__": "__main__"})
        except SystemExit as e:
            code = e.code
    assert code == 1 and "FAILED to re-add Shows" in buf.getvalue() and "name Shows, type tvshows, paths /media/tv" in buf.getvalue()


def test_the_other_jellyfin_steps_are_not_the_rebuild_and_the_rebuild_is_not_theirs():
    organize = {"title": "Configure Jellyfin libraries",
                "prompt_template": "Organize the Jellyfin library into Movies and TV Shows like a streaming service."}
    refresh = {"title": "Trigger metadata refresh", "prompt_template": "POST refresh with ReplaceAllMetadata=true for each Jellyfin library."}
    census = {"title": "Verify Name", "prompt_template": "Verify every Jellyfin library item has a Name."}
    for n in (organize, refresh, census):
        assert not rewrite_rebuild_jellyfin_library(n, ["x"], ["y"], SERVICES, INV)[2], n["title"]
    assert rewrite_organize_jellyfin(organize, ["x"], ["y"], SERVICES, INV)[2]
    assert not rewrite_refresh_jellyfin_metadata(FIX1, ["x"], ["y"], SERVICES, INV)[2]
    assert not rewrite_jellyfin_metadata_report(FIX1, ["x"], ["y"], SERVICES, INV)[2]
    assert not rewrite_rebuild_jellyfin_library(FIX1, ["x"], ["y"], [], {"names": {}})[2]    # no guest → the model's


def test_frame_run_runs_the_rebuild_rewrite_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_rebuild_jellyfin_library(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_rebuild_jellyfin_library(node, cmds") < src.index("read_on_the_machine(cmds")
