"""§17.1643 — a check that compares Jellyfin with another app is not a Jellyfin census.

Live (job 5db1b088, 2026-10-18): the planner split "Verify Jellyfin library paths" into T2 "Verify Movies points to
the folder Radarr imports into" and T2v2 "… Shows … Sonarr …". §17.1554's census claimed both -- "Jellyfin" from the
splitter's "(split from T2: Verify Jellyfin library paths)", "library" from the same note -- and would have printed
Jellyfin's own table and recorded each done without ever reading Radarr's or Sonarr's root folder.
"""
from __future__ import annotations

from app.modules.supervised_runs import rewrite_jellyfin_metadata_report


class _Svc:
    def __init__(self, name, guest, ports):
        self.name, self.guest, self.ports = name, guest, ports


SERVICES = [_Svc("jellyfin", "101", ("8096",)), _Svc("radarr", "103", ("7878",)), _Svc("sonarr", "104", ("8989",))]
INV = {"names": {"101": "jellyfin", "103": "radarr", "104": "sonarr"}}
T2 = {"node_key": "T2", "title": "Verify Movies points to the folder Radarr imports into",
      "prompt_template": "Verify: Movies points to the folder Radarr imports into. (split from T2: Verify Jellyfin library paths)"}
T2V2 = {"node_key": "T2v2", "title": "Verify Shows points to the folder Sonarr imports into",
        "prompt_template": "Verify: Shows points to the folder Sonarr imports into. (split from T2: Verify Jellyfin library paths)"}
T1 = {"node_key": "T1", "title": "Verify Movies library has type 'movies'",
      "prompt_template": "Query Jellyfin API for library types. Movies must be type movies."}
T3 = {"node_key": "T3", "title": "Verify Jellyfin series resolution",
      "prompt_template": "Query Jellyfin API for all series in Shows library. Confirm each resolves as Series/Season/Episode."}


def _claimed(node) -> bool:
    cmds, _, notes = rewrite_jellyfin_metadata_report(node, ["echo draft"], [], SERVICES, INV)
    return bool(notes) and cmds != ["echo draft"]


def test_a_comparison_with_radarr_or_sonarr_is_left_to_its_own_draft():
    assert not _claimed(T2) and not _claimed(T2V2)


def test_a_jellyfin_only_check_is_still_the_census():
    assert _claimed(T1) and _claimed(T3)


def test_a_storage_check_whose_text_mentions_jellyfin_is_not_a_census():
    """§17.1660 — live (job 5db1b088 T24)."""
    t24 = {"node_key": "T24", "title": "Verify shared storage paths",
           "prompt_template": ("Verify the download folder, the *arr root folders and the Jellyfin library folders are "
                               "the same shared storage seen at matching paths.")}
    assert not _claimed(t24)
