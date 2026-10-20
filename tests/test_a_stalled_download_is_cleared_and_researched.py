r"""§17.1515 — "remove the stalled downloads and re-search", done deterministically.

Live (job aefaef87, 2026-10-12): 120 of 238 Sonarr queue items were "stalled with no
connections" (dead releases, no seeders); the operator asked to remove the stale ones and
pull new sources. The engine clears them through Sonarr's queue API — a single bulk DELETE
with blocklist=true (so the dead release is not re-grabbed) and removeFromClient=true (drop
the partials from qBittorrent) — then triggers MissingEpisodeSearch. It must NOT touch
import-blocked items (those downloaded fine and are stuck on episode naming).
"""
from __future__ import annotations

import io
import json
import subprocess
import contextlib
import unittest.mock as mock
import urllib.request as ur

from app.modules.supervised_runs import rewrite_clear_stalled, _CLEAR_STALLED_PY
from app.modules import machine_values as mv

SERVICES = [type("S", (), {"name": "sonarr", "guest": "104", "ports": ("8989",)})()]
INV = {"names": {"104": "sonarr"}}
NODE = {"title": "Remove stalled from Sonarr",
        "prompt_template": "Remove the stalled/dead downloads from Sonarr, blocklisting them, then re-search."}


def test_the_step_is_rewritten_to_the_queue_api_in_the_guest():
    cmds, verify, notes = rewrite_clear_stalled(NODE, ["<model>"], ["chk"], SERVICES, INV)
    assert notes and "§17.1515" in notes[0]["why"]
    assert cmds[0].startswith("pct exec 104 -- sh -c '") and "/api/v3/queue" in cmds[0] and "<SONARR_API_KEY>" in cmds[0]


def test_the_key_fills_and_both_commands_parse():
    cmds, verify, _ = rewrite_clear_stalled(NODE, ["x"], ["y"], SERVICES, INV)
    c, v, _ = mv.read_on_the_machine(cmds, verify, INV)
    assert "<SONARR_API_KEY>" not in c[0] and "/var/lib/sonarr/config.xml" in c[0]
    assert subprocess.run(["bash", "-n", "-c", c[0]], capture_output=True, text=True).returncode == 0
    assert subprocess.run(["bash", "-n", "-c", v[0]], capture_output=True, text=True).returncode == 0


def test_the_verify_is_read_only_safe():
    from app.modules.assist_state_check import read_only_command
    _, verify, _ = rewrite_clear_stalled(NODE, ["x"], ["true"], SERVICES, INV)
    v = verify[0]
    assert v != "true" and "python3" not in v and "$(" not in v and read_only_command(v)


def _run_stub(queue, *args):
    calls = []

    def fake(req, timeout=None):
        calls.append((req.get_method(), req.full_url, req.data and json.loads(req.data)))
        if req.get_method() == "GET":
            return io.BytesIO(json.dumps(queue).encode())
        return io.BytesIO(b"")

    with mock.patch.object(ur, "urlopen", fake):
        import sys
        sys.argv = ["-c", "thekey", *args]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(_CLEAR_STALLED_PY, {"__name__": "__main__"})
        return buf.getvalue(), calls


def test_only_stalled_items_are_bulk_removed_with_blocklist_then_a_research():
    queue = {"records": [
        {"id": 1, "status": "warning", "trackedDownloadStatus": "warning",
         "statusMessages": [{"messages": ["The download is stalled with no connections"]}]},
        {"id": 2, "status": "downloading", "trackedDownloadStatus": "ok"},
        {"id": 3, "trackedDownloadState": "importBlocked", "status": "completed",
         "statusMessages": [{"messages": ["Episode 26x02 was unexpected"]}]},
        {"id": 4, "errorMessage": "The download is stalled with no connections"},
    ], "totalRecords": 4}
    out, calls = _run_stub(queue)
    assert "STALLED: 2 of 4" in out and "REMOVED 2" in out and "searches the 2 removed episodes again" in out
    dels = [c for c in calls if c[0] == "DELETE"]
    assert dels and dels[0][2]["ids"] == [1, 4], "only the no-seeder stalls; the importBlocked item is spared"
    assert "blocklist=true" in dels[0][1] and "removeFromClient=true" in dels[0][1]
    assert "skipRedownload=false" in dels[0][1]
    # §17.1709 — no library-wide MissingEpisodeSearch: skipRedownload=false re-searches exactly these episodes
    assert not [c for c in calls if c[0] == "POST"]


def test_an_import_blocked_item_is_never_blocklisted():
    out, calls = _run_stub({"records": [
        {"id": 9, "trackedDownloadState": "importBlocked", "status": "warning",
         "statusMessages": [{"messages": ["Episode 26x03 was unexpected"]}]}], "totalRecords": 1})
    assert "STALLED: 0 of 1" in out
    assert not [c for c in calls if c[0] == "DELETE"], "a good-but-unimportable download must not be discarded"


def test_a_non_stalled_step_is_left_to_the_model():
    node = {"title": "Add general TV indexers", "prompt_template": "add indexers to prowlarr"}
    out, _, notes = rewrite_clear_stalled(node, ["x"], ["y"], SERVICES, INV)
    assert out == ["x"] and not notes


def test_frame_run_runs_clear_stalled_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_clear_stalled(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_clear_stalled(node, cmds") < src.index("read_on_the_machine(cmds")



def _ago(h):
    import datetime
    return (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=h)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_a_magnet_that_never_fetched_its_metadata_is_stalled_past_the_steps_age():
    """§17.1709 — live (job e5e22b92 MD1): 15 queue items titled by a bare hash, 12 older than 6 h"""
    old = [{"id": 100 + i, "title": "%040x" % (0xabc + i), "status": "queued", "trackedDownloadState": "downloading",
            "statusMessages": [], "added": _ago(186)} for i in range(12)]
    young = [{"id": 200 + i, "title": "%040x" % (0xdef + i), "status": "queued", "trackedDownloadState": "downloading",
              "statusMessages": [], "added": _ago(2.6)} for i in range(3)]
    ok = [{"id": 300, "title": "Show.S01E01.1080p", "status": "downloading", "added": _ago(200)},
          {"id": 301, "title": "%040x" % 0x123, "trackedDownloadState": "importBlocked", "added": _ago(200)}]
    out, calls = _run_stub({"records": old + young + ok, "totalRecords": 17}, "6")
    dels = [c for c in calls if c[0] == "DELETE"]
    assert "STALLED: 12 of 17 queue items (added more than 6 h ago)" in out
    assert dels and sorted(dels[0][2]["ids"]) == [100 + i for i in range(12)]
    out2, calls2 = _run_stub({"records": old + young + ok, "totalRecords": 17})
    assert "STALLED: 15 of 17" in out2                                   # no age in the step: every stuck magnet


def test_the_steps_age_reaches_the_program():
    node = {"title": "Remove the stuck metadata-less torrents through Sonarr",
            "prompt_template": "select the items whose title is a bare hash AND that were added more than 6 hours ago"}
    cmds, _v, _n = rewrite_clear_stalled(node, ["x"], ["y"], SERVICES, INV)
    assert cmds[0].rstrip("'").endswith('"<SONARR_API_KEY>" "6"'), cmds[0][-60:]


def test_a_quoted_series_scopes_the_clear_to_that_show():
    """§17.1710 — "clear the stalled 'How I Met Your Mother' downloads" must not clear every show's stalls"""
    himym = {"title": "How I Met Your Mother"}
    recs = [{"id": 1, "title": "How.I.Met.Your.Mother.S06E20.720p", "series": himym, "status": "warning", "added": _ago(3),
             "statusMessages": [{"messages": ["The download is stalled with no connections"]}]},
            {"id": 2, "title": "%040x" % 0xc1a9, "series": himym, "status": "queued", "added": _ago(3), "statusMessages": []},
            {"id": 3, "title": "Power.Rangers.S26E01", "series": {"title": "Power Rangers"}, "added": _ago(3),
             "statusMessages": [{"messages": ["The download is stalled with no connections"]}]},
            {"id": 4, "title": "How.I.Met.Your.Mother.S06E07.720p", "series": himym, "status": "downloading", "added": _ago(3)}]
    out, calls = _run_stub({"records": recs, "totalRecords": 4}, "--series=How I Met Your Mother")
    dels = [c for c in calls if c[0] == "DELETE"]
    assert "STALLED: 2 of 4 queue items of how i met your mother" in out
    assert sorted(dels[0][2]["ids"]) == [1, 2]
    node = {"title": "Clear the stalled HIMYM downloads",
            "prompt_template": "Remove the stalled downloads of 'How I Met Your Mother' through Sonarr's queue."}
    cmds, _v, _n = rewrite_clear_stalled(node, ["x"], ["y"], SERVICES, INV)
    assert '"--series=How I Met Your Mother"' in cmds[0]
