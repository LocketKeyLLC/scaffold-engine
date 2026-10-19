"""§17.1655 — a backup step is never "already met" off a backup that already exists (job d89078ed T7)."""
from __future__ import annotations

import inspect

from app.modules import supervised_runs as sr


def test_backup_titles_are_taken_now():
    for t in ("Backup Prowlarr config database", "Back up Radarr's config", "CT 102: back up the database",
              "Snapshot the Jellyfin library DB", "Copy config.xml aside before the update"):
        assert sr.a_backup_is_taken_now({"title": t}), t
    for t in ("Verify the backup exists", "Restore Radarr from backup", "Update Prowlarr to 2.6.5", "Check backups"):
        assert sr.a_backup_is_taken_now({"title": t}) == "", t


def test_wired_into_already_met():
    src = inspect.getsource(sr.already_met)
    assert "_blind = _blind or a_backup_is_taken_now(node)" in src
