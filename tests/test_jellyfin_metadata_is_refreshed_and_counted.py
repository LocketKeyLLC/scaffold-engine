r"""§17.1553/§17.1554 — the engine refreshes Jellyfin's metadata/images and counts what is still missing.

Live (job 263e920a, "Jellyfin Metadata and Image Refresh Verification"): every MCP node the planner wrote
spliced other nodes' outputs in as bare `${T1}`/`${T2}`/`${T3}` (the key, the library ids, the item list),
used `curl -s` that cannot report a 401, and carried no check. The engine knows the whole shape: read the
key on the machine, list the typed libraries through VirtualFolders, POST `/Items/{ItemId}/Refresh` with
the ReplaceAll flags the step names (§17.1553), and read the per-item census back in-guest — Name,
Overview, Primary image — with a COMPLETE / N-lacking verdict (§17.1554). A WAIT step becomes bounded
phases that fail honestly when the refresh is still moving, never a `while true` on the 180 s budget.
"""
from __future__ import annotations

import contextlib
import io
import json
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

from app.modules.supervised_runs import (
    rewrite_refresh_jellyfin_metadata, rewrite_jellyfin_metadata_report, rewrite_organize_jellyfin,
    rewrite_completeness_check, _JELLYFIN_REFRESH_PY, _JELLYFIN_CENSUS_PY, _jellyfin_refresh_mode,
    loops_the_network_without_a_budget, loop_dies_on_one_dead_party,
)
from app.modules import machine_values as mv


class _Svc:
    def __init__(self, name, guest, ports):
        self.name, self.guest, self.ports = name, guest, ports


SERVICES = [_Svc("jellyfin", "101", ("8096",)), _Svc("sonarr", "104", ("8989",))]
INV = {"names": {"101": "jellyfin", "104": "sonarr"}}


def _mcp(key, title, prompt, command, tool="run_supervised"):
    """The live nodes' shape: tool=MCP, the command in tool_config (description empty)."""
    return {"node_key": key, "title": title, "prompt_template": prompt, "description": "", "tool": "MCP",
            "tool_config": json.dumps({"server": "pve-runner", "tool": tool,
                                       "args": {"node": "101", "command": command}})}


T4 = _mcp("T4", "Trigger metadata refresh",
          "POST refresh with ReplaceAllMetadata=true for each library. Does not replace images yet.",
          "for lib in ${T2}; do curl -s -X POST -H 'X-Emby-Token: ${T1}' \"http://192.168.1.20:8096/Items/$lib/Refresh\"; done")
T5 = _mcp("T5", "Trigger image refresh", "POST refresh with ReplaceAllImages=true for each library.",
          "for lib in ${T2}; do curl -s -X POST -H 'X-Emby-Token: ${T1}' \"http://192.168.1.20:8096/Items/$lib/Refresh\"; done")
T6 = _mcp("T6", "Wait for refresh completion", "Poll /ScheduledTasks until RefreshLibrary state is Idle.",
          "while true; do state=$(curl -s -H 'X-Emby-Token: ${T1}' 'http://192.168.1.20:8096/ScheduledTasks'); done")
T7 = _mcp("T7", "Verify: Name", "Verify: Name. (split from T7: Verify item metadata completeness)",
          "for item in ${T3}; do curl -s -H 'X-Emby-Token: ${T1}' \"http://192.168.1.20:8096/Items/$item?Fields=Name,Overview,ImageTags\"; done",
          tool="run_readonly")
T3 = _mcp("T3", "Enumerate all items", "Enumerate all series, seasons, episodes with current metadata state.",
          "for lib in ${T2}; do curl -s -H 'X-Emby-Token: ${T1}' \"http://192.168.1.20:8096/Items?ParentId=$lib\"; done",
          tool="run_readonly")
ORGANIZE = {"title": "Configure Jellyfin libraries",
            "prompt_template": "Organize the Jellyfin library into Movies and TV Shows like a streaming "
                               "service with posters and metadata."}
COMPLETENESS = {"title": "Check Scrubs completeness",
                "prompt_template": "Query Sonarr for Scrubs. Compare existing episodes against all seasons."}

LIBS = [{"Name": "Movies", "CollectionType": "movies", "ItemId": "aaa", "Locations": ["/media/movies"]},
        {"Name": "Shows", "CollectionType": "tvshows", "ItemId": "bbb", "Locations": ["/media/tv"]},
        {"Name": "Music", "CollectionType": "music", "ItemId": "ccc", "Locations": ["/media/music"]}]
def _ago(**kw):
    """A Jellyfin-shaped UTC timestamp this many seconds/hours ago — built at CALL time, not import
    time: CI runs the module minutes after collection, and a 'recent' refresh aged past the 3-minute
    window made the wait settle at once (the first CI run of this file)."""
    return (datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%S.0000000Z")


RECENT = _ago(seconds=30)
OLD = _ago(hours=2)
ITEMS = {
    "aaa": [{"Id": "m1", "Type": "Movie", "Name": "Sintel", "Overview": "A girl", "ImageTags": {"Primary": "x"}, "DateLastRefreshed": OLD}],
    "bbb": [{"Id": "s1", "Type": "Series", "Name": "Scrubs", "Overview": "Hospital", "ImageTags": {"Primary": "x"}, "DateLastRefreshed": OLD},
            {"Id": "se1", "Type": "Season", "Name": "Season 1", "SeriesName": "Scrubs", "Overview": "", "ImageTags": {"Primary": "x"}, "DateLastRefreshed": OLD},
            {"Id": "e1", "Type": "Episode", "Name": "My First Day", "SeriesName": "Scrubs", "ParentIndexNumber": 1, "IndexNumber": 1,
             "Overview": "JD starts", "ImageTags": {"Primary": "x"}, "DateLastRefreshed": OLD},
            {"Id": "e2", "Type": "Episode", "Name": "", "SeriesName": "Scrubs", "ParentIndexNumber": 1, "IndexNumber": 4,
             "Overview": None, "ImageTags": {}, "DateLastRefreshed": OLD}],
}


# ── §17.1553 the refresh ──────────────────────────────────────────────────────────────────────────

def test_the_refresh_step_is_rewritten_with_the_mode_the_step_names():
    cmds, verify, notes = rewrite_refresh_jellyfin_metadata(T4, ["<model>"], ["chk"], SERVICES, INV)
    assert notes and "§17.1553" in notes[0]["why"] and "metadata refresh" in notes[0]["why"]
    assert len(cmds) == 1 and cmds[0].startswith("pct exec 101 -- sh -c '")
    assert '"<JELLYFIN_API_KEY>" "metadata"' in cmds[0] and "/Refresh?" in cmds[0]
    cmds5, _, notes5 = rewrite_refresh_jellyfin_metadata(T5, ["x"], ["y"], SERVICES, INV)
    assert notes5 and '"<JELLYFIN_API_KEY>" "images"' in cmds5[0]
    both = {"title": "Refresh Jellyfin metadata and posters", "prompt_template": "pull fresh metadata and artwork for every library"}
    cmdsb, _, notesb = rewrite_refresh_jellyfin_metadata(both, ["x"], ["y"], SERVICES, INV)
    assert notesb and '"<JELLYFIN_API_KEY>" "both"' in cmdsb[0]


def test_refresh_mode_reads_the_flags_first_then_the_words():
    assert _jellyfin_refresh_mode("POST with ReplaceAllMetadata=true. Does not replace images yet.") == "metadata"
    assert _jellyfin_refresh_mode("ReplaceAllImages=true") == "images"
    assert _jellyfin_refresh_mode("ReplaceAllMetadata=true ReplaceAllImages=true") == "both"
    assert _jellyfin_refresh_mode("refresh the posters and cover art") == "images"
    assert _jellyfin_refresh_mode("refresh the metadata and episode names") == "metadata"
    assert _jellyfin_refresh_mode("refresh jellyfin") == "both"


def test_the_refresh_verify_is_read_only_safe_and_carries_no_key():
    from app.modules.assist_state_check import read_only_command
    _, verify, _ = rewrite_refresh_jellyfin_metadata(T4, ["x"], ["true"], SERVICES, INV)
    v = verify[0]
    assert "JELLYFIN_API_KEY" not in v and "python3" not in v and "$(" not in v
    assert "/System/Info/Public" in v and "will be refreshed" in v and read_only_command(v)


def test_the_refresh_key_fills_and_parses_and_passes_the_loop_gates():
    cmds, verify, _ = rewrite_refresh_jellyfin_metadata(T4, ["x"], ["y"], SERVICES, INV)
    c, v, _ = mv.read_on_the_machine(cmds, verify, INV)
    assert "<JELLYFIN_API_KEY>" not in c[0] and "jellyfin.db" in c[0] and not re.search(r"<[A-Z][A-Z0-9_]+>", c[0])
    assert subprocess.run(["bash", "-n", "-c", c[0]], capture_output=True, text=True).returncode == 0
    assert subprocess.run(["bash", "-n", "-c", v[0]], capture_output=True, text=True).returncode == 0
    assert loops_the_network_without_a_budget(cmds) == [] and loop_dies_on_one_dead_party(cmds) == []


def _jellyfin(monkeypatch, argv, script, libs=LIBS, items=ITEMS, items_seq=None):
    import urllib.request as ur
    calls = []
    passes = {"n": 0}          # one census pass = one VirtualFolders read

    def fake_urlopen(req, timeout=None):
        m, u = req.get_method(), req.full_url
        calls.append((m, u))
        if u.endswith("/Library/VirtualFolders"):
            passes["n"] += 1
            return io.BytesIO(json.dumps(libs).encode())
        if "/Items?" in u and m == "GET":
            lid = re.search(r"ParentId=([^&]+)", u).group(1)
            src = items
            if items_seq:
                src = items_seq[min(max(passes["n"] - 1, 0), len(items_seq) - 1)]
            rows = src.get(lid, [])
            if "Recursive=true" not in u:          # the top-level read: series/movies only, as Jellyfin would answer
                rows = [i for i in rows if i.get("Type") in ("Series", "Movie")]
            return io.BytesIO(json.dumps({"Items": rows, "TotalRecordCount": len(rows)}).encode())
        return io.BytesIO(b"")

    monkeypatch.setattr(ur, "urlopen", fake_urlopen)
    slept = []
    monkeypatch.setattr(time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr("sys.argv", ["-c"] + argv)
    buf = io.StringIO()
    code = 0
    with contextlib.redirect_stdout(buf):
        try:
            exec(script, {"__name__": "__main__"})
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 1
    return buf.getvalue(), calls, code, slept


def test_the_refresh_queues_each_typed_library_with_the_right_flags_and_prints_the_before_census(monkeypatch):
    out, calls, code, _ = _jellyfin(monkeypatch, ["k", "metadata"], _JELLYFIN_REFRESH_PY)
    assert code == 0
    assert "BEFORE Movies: 1 items, 0 lacking" in out and "BEFORE Shows: 4 items, 2 lacking" in out
    posts = [u for m, u in calls if m == "POST"]
    assert len(posts) == 2 and all("/Items/" in u and "/Refresh?" in u for u in posts)
    assert all("MetadataRefreshMode=FullRefresh" in u and "ReplaceAllMetadata=true" in u for u in posts)
    assert all("ImageRefreshMode=None" in u and "ReplaceAllImages=false" in u for u in posts)
    assert not any("/Items/ccc/" in u for u in posts)             # the music library is not typed Movies/TV
    assert "REFRESH QUEUED: Movies" in out and "RESULT: metadata refresh queued for 2 libraries" in out
    out_i, calls_i, _, _ = _jellyfin(monkeypatch, ["k", "images"], _JELLYFIN_REFRESH_PY)
    assert all("ImageRefreshMode=FullRefresh" in u and "ReplaceAllImages=true" in u and "ReplaceAllMetadata=false" in u
               for m, u in calls_i if m == "POST")


def test_the_refresh_dry_run_posts_nothing_and_no_typed_library_fails(monkeypatch):
    out, calls, code, _ = _jellyfin(monkeypatch, ["k", "both", "dry"], _JELLYFIN_REFRESH_PY)
    assert code == 0 and "WOULD REFRESH: Movies" in out and not any(m == "POST" for m, _ in calls)
    out2, _, code2, _ = _jellyfin(monkeypatch, ["k", "both"], _JELLYFIN_REFRESH_PY, libs=[LIBS[2]])
    assert code2 == 1 and "no typed Movies/TV library" in out2


def test_a_verify_or_wait_title_is_not_the_refresh_and_the_organize_step_is_not_hijacked():
    for n in (T6, T7, T3, ORGANIZE):
        out, _, notes = rewrite_refresh_jellyfin_metadata(n, ["x"], ["y"], SERVICES, INV)
        assert out == ["x"] and not notes, n["title"]
    assert rewrite_organize_jellyfin(ORGANIZE, ["x"], ["y"], SERVICES, INV)[2]   # §17.1510 still its own


# ── §17.1554 the census / wait ─────────────────────────────────────────────────────────────────────

def test_a_verify_step_becomes_the_census_and_a_wait_step_becomes_bounded_phases():
    cmds, verify, notes = rewrite_jellyfin_metadata_report(T7, ["<model>"], ["chk"], SERVICES, INV)
    assert notes and "§17.1554" in notes[0]["why"]
    assert len(cmds) == 1 and '"<JELLYFIN_API_KEY>" "report"' in cmds[0] and cmds[0].startswith("pct exec 101 -- sh -c '")
    c6, _, n6 = rewrite_jellyfin_metadata_report(T6, ["x"], ["y"], SERVICES, INV)
    assert n6 and len(c6) == 7 and all('"wait"' in c for c in c6[:6]) and '"last"' in c6[6]
    assert "while true" not in " ".join(c6)
    c3, _, n3 = rewrite_jellyfin_metadata_report(T3, ["x"], ["y"], SERVICES, INV)
    assert n3 and len(c3) == 1 and '"report"' in c3[0]


def test_the_census_verify_carries_no_key_and_the_key_fills_in_the_run():
    from app.modules.assist_state_check import read_only_command
    cmds, verify, _ = rewrite_jellyfin_metadata_report(T7, ["x"], ["y"], SERVICES, INV)
    assert "JELLYFIN_API_KEY" not in verify[0] and read_only_command(verify[0])
    c, v, _ = mv.read_on_the_machine(cmds, verify, INV)
    assert "<JELLYFIN_API_KEY>" not in c[0] and "jellyfin.db" in c[0] and not re.search(r"<[A-Z][A-Z0-9_]+>", c[0])
    assert subprocess.run(["bash", "-n", "-c", c[0]], capture_output=True, text=True).returncode == 0
    assert loops_the_network_without_a_budget(cmds) == [] and loop_dies_on_one_dead_party(cmds) == []


def test_the_census_reports_per_type_counts_the_lacking_items_and_a_verdict(monkeypatch):
    out, calls, code, slept = _jellyfin(monkeypatch, ["k", "report"], _JELLYFIN_CENSUS_PY)
    assert code == 0 and slept == []
    # the library's declared type and what its top level resolved to — the first thing wrong when a
    # TV library shows a flat list of "movies" (live: Shows type=tvshows yet 902 Movie items)
    assert "LIBRARY Shows type=tvshows paths=/media/tv" in out and "top level: Series x1" in out
    assert "top: Series Scrubs" in out and "sample lacking: Season Season 1" in out
    assert "Movies/Movie: 1, missing name 0, overview 0, poster 0" in out
    assert "Shows/Episode: 2, missing name 1, overview 1, poster 1" in out
    assert "Shows/Season: 1, missing name 0, overview 1, poster 0" in out
    assert "LACKING: Season Scrubs / Season 1 -- no overview" in out
    assert "LACKING: Episode Scrubs S01E04 -- no name, overview, poster" in out
    assert "RESULT: 2 item(s) still lack a name, overview or poster" in out
    assert not any(m == "POST" for m, _ in calls)                 # a read, never a change


def test_a_tv_library_whose_items_resolved_as_movies_fails_the_step_plainly(monkeypatch):
    """Live (2026-10-14): Shows is typed tvshows, yet all 902 items under it were Movie — a flat list of
    'movies' named like episodes, no Series/Season/Episode. That is not a metadata gap a refresh can fix;
    the census says so and FAILS (exit 2), so the engine's failure research (§17.1201) takes it from here."""
    movies_in_tv = {"aaa": ITEMS["aaa"],
                    "bbb": [{"Id": "x%d" % i, "Type": "Movie", "Name": "Scrubs - S01E0%d - Ep" % i, "Overview": "",
                             "ImageTags": {}, "Path": "/media/tv/Scrubs/Season 1/Scrubs - S01E0%d.mkv" % i} for i in range(1, 4)]}
    out, _, code, _ = _jellyfin(monkeypatch, ["k", "report"], _JELLYFIN_CENSUS_PY, items=movies_in_tv)
    assert code == 2
    assert "WRONG TYPES: Shows is a TV library (tvshows) but Jellyfin resolved its 3 items as Movie" in out
    assert "RESULT: 3 item(s) still lack" in out                 # the census is still printed in full


def test_a_complete_library_says_complete(monkeypatch):
    full = {"aaa": ITEMS["aaa"], "bbb": ITEMS["bbb"][:1]}
    out, _, code, _ = _jellyfin(monkeypatch, ["k", "report"], _JELLYFIN_CENSUS_PY, items=full)
    assert code == 0 and "RESULT: COMPLETE" in out and "LACKING" not in out


def test_the_wait_phase_settles_at_once_when_nothing_was_refreshed_recently(monkeypatch):
    out, _, code, slept = _jellyfin(monkeypatch, ["k", "wait"], _JELLYFIN_CENSUS_PY)
    assert code == 0 and slept == [] and "REFRESH SETTLED" in out and "RESULT:" in out


def test_the_wait_phase_polls_while_items_keep_refreshing_and_the_last_phase_fails(monkeypatch):
    def moving(ts):
        return {"aaa": [dict(ITEMS["aaa"][0], DateLastRefreshed=ts)], "bbb": ITEMS["bbb"]}
    seq = [moving(_ago(seconds=30)), moving(_ago(seconds=20)), moving(_ago(seconds=10))]
    out, _, code, slept = _jellyfin(monkeypatch, ["k", "wait"], _JELLYFIN_CENSUS_PY, items_seq=seq)
    assert code == 0 and slept == [40, 40] and "STILL REFRESHING" in out        # a wait phase lets the next run
    out2, _, code2, _ = _jellyfin(monkeypatch, ["k", "last"], _JELLYFIN_CENSUS_PY, items_seq=seq)
    assert code2 == 1 and "STILL REFRESHING" in out2                             # the last phase fails honestly


def test_without_server_timestamps_the_wait_polls_by_counts(monkeypatch):
    """Live: this Jellyfin returned no DateLastRefreshed at all, so a timestamp-only wait 'settled' at
    once with 902 items still unidentified. With no timestamps the wait reads the library twice 40 s
    apart and calls it settled only when nothing changed."""
    def strip(src):
        return {k: [dict(i, DateLastRefreshed=None) for i in v] for k, v in src.items()}
    steady = strip(ITEMS)
    out, _, code, slept = _jellyfin(monkeypatch, ["k", "wait"], _JELLYFIN_CENSUS_PY, items_seq=[steady, steady])
    assert code == 0 and slept == [40] and "REFRESH SETTLED" in out
    fewer = strip({"aaa": ITEMS["aaa"], "bbb": ITEMS["bbb"][:3]})
    out2, _, code2, slept2 = _jellyfin(monkeypatch, ["k", "last"], _JELLYFIN_CENSUS_PY, items_seq=[steady, fewer, steady])
    assert code2 == 1 and slept2 == [40, 40] and "STILL REFRESHING" in out2


def test_the_wait_phase_settles_when_the_refresh_stops_moving(monkeypatch):
    steady = {"aaa": [dict(ITEMS["aaa"][0], DateLastRefreshed=_ago(seconds=30))], "bbb": ITEMS["bbb"]}
    out, _, code, slept = _jellyfin(monkeypatch, ["k", "last"], _JELLYFIN_CENSUS_PY, items_seq=[steady, steady])
    assert code == 0 and slept == [40] and "REFRESH SETTLED" in out and "RESULT:" in out


def test_the_sonarr_completeness_check_and_a_non_jellyfin_step_are_not_the_census():
    out, _, notes = rewrite_jellyfin_metadata_report(COMPLETENESS, ["x"], ["y"], SERVICES, INV)
    assert out == ["x"] and not notes
    assert rewrite_completeness_check(COMPLETENESS, ["x"], ["y"], SERVICES, INV)[2]     # §17.1504 still its own
    assert not rewrite_jellyfin_metadata_report(T4, ["x"], ["y"], SERVICES, INV)[2]      # the refresh is §17.1553's
    assert not rewrite_jellyfin_metadata_report(T7, ["x"], ["y"], [], {"names": {}})[2]  # no guest → the model's


# ── §17.1559 a trigger or a probe-only check is never "already met" ────────────────────────────────

def test_the_pre_pass_cannot_declare_a_trigger_or_a_probe_only_step_already_met():
    """Live: T4/T5 (refresh POSTs), T6 (wait) and T7/T7v2 (census) were all recorded `already met` off
    `jellyfin HTTP 200` before anything ran. A trigger's goal is the work it queues; a liveness probe says
    the service is up, not that the goal holds."""
    from app.modules.supervised_runs import checks_can_witness_the_change, sends_a_change, _status_only_probe
    r_cmds, r_verify, _ = rewrite_refresh_jellyfin_metadata(T4, ["x"], ["y"], SERVICES, INV)
    why = checks_can_witness_the_change(r_cmds, r_verify)
    assert why and "sends a change" in why and "POST" in why
    c_cmds, c_verify, _ = rewrite_jellyfin_metadata_report(T7, ["x"], ["y"], SERVICES, INV)
    why2 = checks_can_witness_the_change(c_cmds, c_verify)
    assert why2 and "liveness probe" in why2
    w_cmds, w_verify, _ = rewrite_jellyfin_metadata_report(T6, ["x"], ["y"], SERVICES, INV)
    assert "liveness probe" in checks_can_witness_the_change(w_cmds, w_verify)
    # the shapes the rules key on
    assert sends_a_change(["curl -s -X POST http://x/Items/1/Refresh"]) == "`POST`"
    assert sends_a_change(["curl -s -d '{}' http://x/api"]) == "`-d` a request body"
    assert sends_a_change(["python3 -c \"api('/api/v3/command','POST',{'name':'RenameSeries'})\""]) == "`POST`"
    assert sends_a_change(["# never POST here\ncurl -s http://x/Items"]) == ""
    assert _status_only_probe('pct exec 101 -- curl -s -o /dev/null -w "jellyfin HTTP %{http_code}" http://127.0.0.1:8096/System/Info/Public')
    assert _status_only_probe("pct exec 104 -- sh -c 'curl -s -o /dev/null -w \"%{http_code}\" http://x/; echo done'")
    assert not _status_only_probe("pct exec 104 -- curl -s --fail-with-body -H \"X-Api-Key: $K\" http://x/api/v3/config/naming")
    assert not _status_only_probe("pct exec 101 -- systemctl is-active jellyfin")
    assert not _status_only_probe("df -h /")


def test_a_state_step_whose_checks_read_the_state_is_still_a_pre_pass_candidate():
    """§17.1302's own case (ADD84: growpart + resize2fs, confirmed by `df -h /`) and §17.1365's readback case
    are untouched: no change is sent over HTTP, and a key written is read back."""
    from app.modules.supervised_runs import checks_can_witness_the_change
    assert checks_can_witness_the_change(["growpart /dev/sda 1", "resize2fs /dev/sda1"], ["df -h /"]) == ""
    assert checks_can_witness_the_change(["echo 'WebUI\\Port=8080' >> /etc/q.conf"], ["grep '^WebUI.Port=' /etc/q.conf"]) == ""
    # the organize step (§17.1510) POSTs VirtualFolders and reads the type markers back: a trigger, so it RUNS
    # (its script is idempotent) rather than being pre-read done off `ls`
    o_cmds, o_verify, _ = rewrite_organize_jellyfin(ORGANIZE, ["x"], ["y"], SERVICES, INV)
    assert "sends a change" in checks_can_witness_the_change(o_cmds, o_verify)


def test_frame_run_runs_the_jellyfin_rewrites_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    for fn in ("rewrite_refresh_jellyfin_metadata", "rewrite_jellyfin_metadata_report"):
        assert f"{fn}(node, cmds, verify, services, inventory)" in src
        assert src.index(f"{fn}(node, cmds") < src.index("read_on_the_machine(cmds")
