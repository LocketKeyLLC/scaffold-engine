r"""§17.1724 — the bulk clear-stalled (§17.1515) does not claim a step about ONE named download, or a read.

Live (job e5e22b92 SM4 "Remove the stuck Power Rangers S3 Legacy download", 2026-10-21): the step asked for one
torrent, found by its title, removed with `skipRedownload=true`, "touch nothing else". §17.1515 claimed it by its
title: a bulk DELETE of every stalled item, a re-search, and a series filter taken from the release's quoted title
("Power Rangers: Mighty Morphin", which Sonarr's series is not called). Of the nine stored titles the claim matches,
"Verify the 5 stalled downloads are gone from Sonarr queue" -- a read -- was claimed the same way.
"""
from __future__ import annotations

from app.modules.supervised_runs import rewrite_clear_stalled

SERVICES = [type("S", (), {"name": "sonarr", "guest": "104", "ports": ("8989",)})()]
INV = {"names": {"104": "sonarr"}}
SM4 = {"title": "Remove the stuck Power Rangers S3 Legacy download",
       "description": "Change, operator-approved (one download removed). Inside Sonarr's container (CT 104): read "
                      "/api/v3/queue?pageSize=1000&includeUnknownSeriesItems=true once and find the queue records "
                      "whose title starts with \"Power Rangers: Mighty Morphin' S03/Movie 1 (Legacy Collection)\" … "
                      "DELETE /api/v3/queue/{id}?removeFromClient=true&blocklist=true&skipRedownload=true … "
                      "Touch nothing else."}


def _claimed(node):
    return bool(rewrite_clear_stalled(node, ["<model>"], ["chk"], SERVICES, INV)[2])


def test_the_live_step_is_left_to_its_own_draft():
    cmds, verify, notes = rewrite_clear_stalled(SM4, ["<model>"], ["chk"], SERVICES, INV)
    assert notes == [] and cmds == ["<model>"] and verify == ["chk"]


def test_the_stalled_downloads_are_still_claimed():
    for title in ("Remove stalled from Sonarr", "Remove stalled Sonarr downloads and re-search",
                  "Clear stalled Sonarr downloads", "Remove the stuck metadata-less torrents through Sonarr",
                  "Clear the stalled HIMYM downloads", "Remove every stuck download"):
        assert _claimed({"title": title, "prompt_template": "Remove them from Sonarr."}), title


def test_a_read_is_not_claimed():
    assert not _claimed({"title": "Verify the 5 stalled downloads are gone from Sonarr queue"})
    assert not _claimed({"title": "Clear stalled Sonarr downloads",
                         "description": "Read-only, change nothing. List what a clear would remove."})


def test_one_torrent_is_not_claimed():
    assert not _claimed({"title": "Remove the stalled S28 torrent"})
    assert not _claimed({"title": "Cancel the dead Dino Fury pack"})
