r"""§17.1552 — a "rename/organize the episode files into season folders" step makes Sonarr do it,
through its own API: `renameEpisodes=true`, `seasonFolder=true` on every series, one `RenameSeries`
command polled to completion.

Live (Jellyfin metadata job 263e920a, 2026-10-14): the TV library was "not broken down into seasons /
episode names" with wrong posters. Measured on CT 104, Sonarr's naming had `renameEpisodes: false` and
every series folder was FLAT scene names — `Power.Rangers.Super.Megaforce.S01E19…` beside
`…Beast.Morphers.S26E01…` — so Jellyfin read the scene numbers and identified the wrong episodes. The
rename is Sonarr's to do (it knows each file's real season/episode); no Jellyfin refresh can fix a file
that names the wrong episode.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import time

import pytest

from app.modules.supervised_runs import rewrite_sonarr_rename_files, _SONARR_RENAME_PY
from app.modules import machine_values as mv


class _Svc:
    def __init__(self, name, guest, ports):
        self.name, self.guest, self.ports = name, guest, ports


SERVICES = [_Svc("jellyfin", "101", ("8096",)), _Svc("sonarr", "104", ("8989",))]
INV = {"names": {"101": "jellyfin", "104": "sonarr"}}
NODE = {"title": "Rename episode files into season folders",
        "prompt_template": "Have Sonarr rename every episode file and move it into Season folders so "
                           "Jellyfin identifies each episode."}

NAMING = {"renameEpisodes": False, "standardEpisodeFormat": "{Series Title} - S{season:00}E{episode:00} - {Episode Title} {Quality Full}",
          "seasonFolderFormat": "Season {season}", "id": 1}
SERIES = [{"id": 3, "title": "Power Rangers", "seasonFolder": False},
          {"id": 5, "title": "Scrubs", "seasonFolder": True}]


def test_the_step_is_rewritten_to_sonarr_in_its_guest():
    cmds, verify, notes = rewrite_sonarr_rename_files(NODE, ["<model>"], ["chk"], SERVICES, INV)
    assert notes and "§17.1552" in notes[0]["why"]
    assert len(cmds) == 1 and cmds[0].startswith("pct exec 104 -- sh -c '")
    assert "RenameSeries" in cmds[0] and "<SONARR_API_KEY>" in cmds[0] and "X-Api-Key" in cmds[0]


def test_the_verify_reads_the_naming_config_with_the_stored_key_and_is_read_only_safe():
    from app.modules.assist_state_check import read_only_command
    _, verify, _ = rewrite_sonarr_rename_files(NODE, ["x"], ["true"], SERVICES, INV)
    v = verify[0]
    assert "/api/v3/config/naming" in v and "$SONARR_API_KEY" in v and "python3" not in v and "$(" not in v
    assert read_only_command(v)


def test_the_key_fills_and_the_command_parses():
    cmds, verify, _ = rewrite_sonarr_rename_files(NODE, ["x"], ["y"], SERVICES, INV)
    c, v, _ = mv.read_on_the_machine(cmds, verify, INV)
    assert "<SONARR_API_KEY>" not in c[0] and not re.search(r"<[A-Z][A-Z0-9_]+>", c[0])
    assert subprocess.run(["bash", "-n", "-c", c[0]], capture_output=True, text=True).returncode == 0
    assert subprocess.run(["bash", "-n", "-c", v[0]], capture_output=True, text=True).returncode == 0


def _run(monkeypatch, argv, naming, series, command_status="completed"):
    import urllib.request as ur
    calls = []
    cmd_polls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        m, u = req.get_method(), req.full_url
        body = json.loads(req.data.decode()) if req.data else None
        calls.append((m, u, body))
        if u.endswith("/api/v3/config/naming") and m == "GET":
            return io.BytesIO(json.dumps(naming).encode())
        if u.endswith("/api/v3/series") and m == "GET":
            return io.BytesIO(json.dumps(series).encode())
        if u.endswith("/api/v3/command") and m == "POST":
            return io.BytesIO(json.dumps({"id": 7, "status": "queued"}).encode())
        if "/api/v3/command/7" in u:
            cmd_polls["n"] += 1
            st = command_status if cmd_polls["n"] >= 2 else "started"
            return io.BytesIO(json.dumps({"id": 7, "status": st}).encode())
        return io.BytesIO(json.dumps(body or {}).encode())

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", lambda s: None)
    monkeypatch.setattr("sys.argv", ["-c"] + argv)
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            exec(_SONARR_RENAME_PY, {"__name__": "__main__"})
        except SystemExit as e:
            code = e.code
    return buf.getvalue(), calls, code


def test_it_turns_on_renaming_sets_season_folders_and_renames_every_series(monkeypatch):
    out, calls, code = _run(monkeypatch, ["thekey"], dict(NAMING), [dict(s) for s in SERIES])
    assert code == 0
    assert "SET: renameEpisodes=true" in out
    assert "SET: Power Rangers seasonFolder=true" in out and "OK: Scrubs already uses season folders" in out
    assert "RENAME: Sonarr command 7 is completed for 2 series" in out and "RESULT: rename completed" in out
    puts = [(u, b) for m, u, b in calls if m == "PUT"]
    assert any(u.endswith("/config/naming/1") and b["renameEpisodes"] is True for u, b in puts)
    assert any(u.endswith("/series/3") and b["seasonFolder"] is True for u, b in puts)
    assert not any(u.endswith("/series/5") for u, _ in puts)          # already right: left alone
    posts = [(u, b) for m, u, b in calls if m == "POST"]
    assert posts == [("http://127.0.0.1:8989/api/v3/command", {"name": "RenameSeries", "seriesIds": [3, 5]})]
    # every call carries the key as a header
    assert all("X-Api-Key" not in u for _, u, _ in calls)   # never in the URL


def test_dry_run_changes_nothing(monkeypatch):
    out, calls, code = _run(monkeypatch, ["k", "dry"], dict(NAMING), [dict(s) for s in SERIES])
    assert code == 0 and "RESULT: dry run, nothing renamed" in out
    assert "WOULD SET: renameEpisodes=true" in out and "WOULD SET: Power Rangers seasonFolder=true" in out
    assert all(m == "GET" for m, _, _ in calls)


def test_already_renaming_is_idempotent(monkeypatch):
    naming = dict(NAMING, renameEpisodes=True)
    out, calls, _ = _run(monkeypatch, ["k"], naming, [dict(SERIES[1])])
    assert "OK: naming already renames episodes" in out
    assert not any(u.endswith("/config/naming/1") for m, u, _ in calls if m == "PUT")


def test_a_failed_rename_fails_the_step(monkeypatch):
    out, _, code = _run(monkeypatch, ["k"], dict(NAMING), [dict(SERIES[0])], command_status="failed")
    assert code != 0 and "RENAME: Sonarr command 7 is failed" in out


def test_the_jellyfin_organize_step_is_not_hijacked():
    """§17.1510's own node: 'Configure Jellyfin libraries' with an 'organize the Jellyfin library'
    prompt names no Sonarr, season-folder or file work — it stays with the organize capability."""
    org = {"title": "Configure Jellyfin libraries",
           "prompt_template": "Organize the Jellyfin library into Movies and TV Shows like a streaming service."}
    out, _, notes = rewrite_sonarr_rename_files(org, ["x"], ["y"], SERVICES, INV)
    assert out == ["x"] and not notes
    org2 = {"title": "Organize Jellyfin library", "prompt_template": "arrange into Movies and TV Shows"}
    assert not rewrite_sonarr_rename_files(org2, ["x"], ["y"], SERVICES, INV)[2]


def test_a_non_rename_step_and_an_unknown_guest_are_left_to_the_model():
    node = {"title": "Queue missing episodes", "prompt_template": "search sonarr for missing episode files"}
    assert not rewrite_sonarr_rename_files(node, ["x"], ["y"], SERVICES, INV)[2]
    assert not rewrite_sonarr_rename_files(NODE, ["x"], ["y"], [], {"names": {}})[2]


def test_the_budget_and_dead_party_gates_accept_the_template():
    """§17.1267/§17.1274 — the loops are bounded slices at timeout=15 (10 × 15 = 150 s) and every
    network call inside a loop is guarded; the gates that killed earlier drafts say nothing here."""
    from app.modules.supervised_runs import loops_the_network_without_a_budget, loop_dies_on_one_dead_party
    cmds, _, _ = rewrite_sonarr_rename_files(NODE, ["x"], ["y"], SERVICES, INV)
    assert loops_the_network_without_a_budget(cmds) == []
    assert loop_dies_on_one_dead_party(cmds) == []


def test_frame_run_runs_the_rename_rewrite_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_sonarr_rename_files(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_sonarr_rename_files(node, cmds") < src.index("read_on_the_machine(cmds")
