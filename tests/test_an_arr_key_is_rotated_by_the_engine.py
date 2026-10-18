"""§17.1645 — rotating an *arr app's API key is the engine's own block (job 5db1b088 RK1, 2026-10-18).

Radarr's key was the literal word RADARR_API_KEY and Prowlarr got 401 on every sync. The model's draft generated the
new key on the host and spliced it into eight `pct exec … sh -c "…<key>…"` lines, checked files the script deleted,
and -- because the old value is also the variable's NAME in `Environment=RADARR_API_KEY=RADARR_API_KEY` -- a plain
replace would have renamed the panel's variable.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from app.modules import arr_key_rotation as akr
from app.modules import supervised_runs as sr
from app.modules.service_truth import ServiceTruth

RK1 = {"node_key": "RK1", "tool": "Shell", "title": "CT 103: rotate Radarr's API key and update every holder",
       "description": (
           "Measured 2026-10-18: Radarr's ApiKey in /var/lib/radarr/config.xml on CT 103 is the literal word "
           "RADARR_API_KEY, and Prowlarr (CT 102) gets 401 Unauthorized on GET http://192.168.1.22:7878/api/v3/indexer. "
           "Operator decision 2026-10-18: rotate the key. 1) Inside CT 103: generate a new random 32-character hex key, "
           "back up config.xml, stop radarr.service, set <ApiKey> to the new key, start radarr.service. 2) Inside CT 102: "
           "update Prowlarr's application named Radarr. 3) Inside CT 111: back up and update RADARR_API_KEY in the "
           "Environment= line of /etc/systemd/system/control-panel.service and radarrApiKey in "
           "/opt/control-panel-backend/config/media-request.json, then systemctl daemon-reload and restart "
           "control-panel.service. Done when Radarr answers 200 on /api/v3/system/status with the new key.")}
GUESTS = {"radarr": "103", "prowlarr": "102", "sonarr": "104"}
SERVICES = [ServiceTruth(guest="103", unit="radarr.service", name="radarr", ports=("7878",)),
            ServiceTruth(guest="102", unit="prowlarr.service", name="prowlarr", ports=("9696",)),
            ServiceTruth(guest="111", unit="control-panel.service", name="control-panel", ports=("3001",),
                         workdir="/opt/control-panel-backend")]
INV = {"vms": {}, "cts": {"102": "running", "103": "running", "111": "running"},
       "names": {"102": "prowlarr", "103": "radarr", "111": "control-panel"}}


def test_the_step_names_one_app_and_its_holders():
    assert akr.rotated_app(RK1) == "radarr"
    assert akr.rotated_app({"title": "Verify Radarr root folders"}) is None
    assert akr.rotated_app({"title": "Rotate the API keys of Radarr and Sonarr"}) is None      # two apps: not one block
    h = akr.holders(RK1["description"], {"103", "102"})
    assert h == {"111": (["/etc/systemd/system/control-panel.service",
                          "/opt/control-panel-backend/config/media-request.json"], ["control-panel.service"])}, h


def test_no_key_value_is_ever_on_a_command_line():
    cmds, verify, _ = akr.block(RK1, GUESTS.get)
    joined = "\n".join(cmds + verify)
    assert "token_hex(16)" in cmds[0] and cmds[0].startswith("pct exec 103 -- python3 -c ")
    assert not re.search(r"\b[0-9a-f]{32}\b", joined)
    assert "--perms 600" in joined and cmds[-1].startswith("rm -rf /root/.scaffold-rotate-radarr")
    assert any(c.startswith("pct exec 102 -- python3 -c ") and "ApplicationIndexerSync" in c for c in cmds)


def test_the_frame_takes_it_and_asks_nothing():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": [], "can_write_files": True}
    draft = "## Run this\n\n```bash\nPROWLARR_API_KEY=\"$PROWLARR_API_KEY\" python3 /tmp/rotate_radarr_key.py\n```\n"
    frame = sr.frame_run(RK1, draft, spec, policy, services=SERVICES, inventory=INV)
    assert not frame["refused"] and frame["inputs"] == [], (frame["refused"], frame["inputs"])
    assert frame["commands"][0].startswith("pct exec 103 -- python3 -c ") and not frame.get("files")


# ── run the three programs against real files ────────────────────────────────

def _run(prog: str, args: list[str], root: Path) -> subprocess.CompletedProcess:
    # the programs use absolute /root and /var/lib paths: rebase them onto a temp dir, and stub systemctl
    src = prog.replace("/root/", f"{root}/root/").replace("/var/lib/", f"{root}/var/lib/") \
              .replace("/etc/systemd/", f"{root}/etc/systemd/").replace("'/config/config.xml'", f"'{root}/none'")
    src = src.replace("subprocess.run(", "(lambda *a, **k: None)(").replace("range(24)", "range(2)")
    return subprocess.run(["python3", "-c", src, *args], capture_output=True, text=True)


def test_the_holder_update_changes_the_value_and_never_the_name(tmp_path):
    (tmp_path / "root").mkdir()
    (tmp_path / "root/.scaffold-rotate.old").write_text("RADARR_API_KEY")
    (tmp_path / "root/.scaffold-rotate.new").write_text("0123456789abcdef0123456789abcdef")
    unit = tmp_path / "etc/systemd/system/control-panel.service"; unit.parent.mkdir(parents=True)
    unit.write_text("[Service]\nEnvironment=RADARR_API_KEY=RADARR_API_KEY\nEnvironment=\"SONARR_API_KEY=abc\"\n")
    cfg = tmp_path / "media-request.json"
    cfg.write_text(json.dumps({"radarrApiKey": "RADARR_API_KEY", "note": "uses RADARR_API_KEY env"}))
    r = _run(akr.HOLDER_PY, [str(unit).replace(str(tmp_path), "") and str(unit), str(cfg), "--"], tmp_path)
    assert r.returncode == 0, r.stderr
    assert "Environment=RADARR_API_KEY=0123456789abcdef0123456789abcdef" in unit.read_text()
    after = json.loads(cfg.read_text())
    assert after == {"radarrApiKey": "0123456789abcdef0123456789abcdef", "note": "uses RADARR_API_KEY env"}
    assert list(unit.parent.glob("control-panel.service.bak.*")) and not (tmp_path / "root/.scaffold-rotate.old").exists()
    assert "0123456789abcdef" not in r.stdout


def test_the_rotation_backs_up_writes_and_never_prints_the_key(tmp_path):
    (tmp_path / "root").mkdir()
    conf = tmp_path / "var/lib/radarr/config.xml"; conf.parent.mkdir(parents=True)
    conf.write_text("<Config>\n  <Port>7878</Port>\n  <ApiKey>RADARR_API_KEY</ApiKey>\n</Config>\n")
    r = _run(akr.ROTATE_PY, ["radarr", "1", "v3"], tmp_path)       # nothing listens on port 1: the wait must FAIL
    new = (tmp_path / "root/.scaffold-rotate.new").read_text()
    assert re.fullmatch(r"[0-9a-f]{32}", new) and f"<ApiKey>{new}</ApiKey>" in conf.read_text()
    assert (tmp_path / "root/.scaffold-rotate.old").read_text() == "RADARR_API_KEY"
    assert list(conf.parent.glob("config.xml.bak.*")) and r.returncode == 1 and "FAILED" in r.stderr
    assert new not in r.stdout + r.stderr
    # a re-run finds its own key in place and does not generate a second one
    r2 = _run(akr.ROTATE_PY, ["radarr", "1", "v3"], tmp_path)
    assert "ALREADY ROTATED" in r2.stdout and (tmp_path / "root/.scaffold-rotate.new").read_text() == new
