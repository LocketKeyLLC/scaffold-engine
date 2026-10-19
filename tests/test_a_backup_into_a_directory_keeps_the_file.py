"""§17.1654 — the engine's backup-before-overwrite line keeps the FILE a cp/mv into a directory replaces.

Live (job d89078ed T8): `cp /var/lib/prowlarr/prowlarr.db /var/backups/prowlarr/` was given
`[ -e "/var/backups/prowlarr/" ] && cp -a "/var/backups/prowlarr/" "/var/backups/prowlarr/.bak.<stamp>"` -- a directory
copied into itself, which fails; under `set -e` the step stopped right after `systemctl stop prowlarr.service`.
"""
from __future__ import annotations

import subprocess

from app.modules.runbook_templates import keep_a_copy_before_overwrites

LIVE = ("set -e\nsystemctl stop prowlarr.service\nmkdir -p /var/backups/prowlarr\n"
        "cp /var/lib/prowlarr/prowlarr.db /var/backups/prowlarr/\nsystemctl start prowlarr.service")


def test_the_backup_names_the_file_inside_the_directory():
    out = keep_a_copy_before_overwrites(LIVE)
    assert 'if [ -f "/var/backups/prowlarr/prowlarr.db" ]; then cp -a "/var/backups/prowlarr/prowlarr.db"' in out, out
    assert 'cp -a "/var/backups/prowlarr/" ' not in out


def test_the_step_runs_to_its_end_at_bash_level(tmp_path):
    src = tmp_path / "lib"; dst = tmp_path / "backups"; src.mkdir(); dst.mkdir()
    (src / "prowlarr.db").write_text("v2"); (dst / "prowlarr.db").write_text("v1")
    script = keep_a_copy_before_overwrites(
        f"set -e\nmkdir -p {dst}\ncp {src}/prowlarr.db {dst}/\necho STARTED")
    r = subprocess.run(["bash", "-c", script], capture_output=True, text=True)
    assert r.returncode == 0 and "STARTED" in r.stdout, r.stderr
    kept = [p for p in dst.iterdir() if p.name.startswith("prowlarr.db.bak.")]
    assert len(kept) == 1 and kept[0].read_text() == "v1" and (dst / "prowlarr.db").read_text() == "v2"
    # a first copy (nothing there yet) also runs to its end
    r2 = subprocess.run(["bash", "-c", keep_a_copy_before_overwrites(f"set -e\ncp {src}/prowlarr.db {tmp_path}/new.db\necho OK")],
                        capture_output=True, text=True)
    assert r2.returncode == 0 and "OK" in r2.stdout, r2.stderr
