"""§17.1656 — "update <arr app> to <version>" is the engine's block, from the tarball install it measured
(job d89078ed T5/T8/T11: apt upgrades drafted for apps that are /opt tarballs, one shape leaving them stopped)."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from app.modules import arr_update as au
from app.modules import supervised_runs as sr
from app.modules.service_truth import ServiceTruth

SONARR = ServiceTruth(guest="104", unit="sonarr.service", name="sonarr", state="active", user="sonarr", group="sonarr",
                      argv="/opt/Sonarr/Sonarr -nobrowser -data=/var/lib/sonarr", data_dir="/var/lib/sonarr",
                      ports=("8989",))
T5 = {"node_key": "T5", "title": "Update Sonarr to 4.0.20.3014", "tool": "Shell",
      "prompt_template": "Update Sonarr in CT 104 to 4.0.20.3014 after the backup."}


def test_it_claims_an_update_of_a_measured_tarball_install():
    assert au.updated_app(T5) == ("sonarr", "4.0.20.3014")
    cmds, verify, note = au.block(T5, [SONARR])
    assert "Sonarr.main.4.0.20.3014.linux-x64.tar.gz" in cmds[0] and "apt" not in " ".join(cmds)
    assert "curl" in cmds[0] and "systemctl stop" not in cmds[0] and "systemctl stop" in cmds[1]   # fetched while it runs
    assert "/var/backups/sonarr" in cmds[1] and "-C /opt/Sonarr --strip-components=1" in cmds[1]
    assert "chown -R sonarr:sonarr /opt/Sonarr" in cmds[1] and cmds[1].rstrip("'").split("; ")[-3].startswith("systemctl start")
    assert cmds[2].startswith("pct exec 104 -- python3 -c ") and cmds[2].endswith("8989 v3 4.0.20.3014 /var/lib/sonarr/config.xml")


def test_what_it_leaves_alone():
    pkg = ServiceTruth(guest="104", unit="sonarr.service", name="sonarr", argv="/usr/bin/sonarr -data=/var/lib/sonarr")
    assert au.block(T5, [pkg]) is None                                       # a packaged install: not this shape
    assert au.block({"title": "Update Sonarr"}, [SONARR]) is None            # no version named
    assert au.block({"title": "Verify Sonarr API version"}, [SONARR]) is None


def test_the_frame_takes_it():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {"X-Runner-Token": "t"}
    policy = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": []}
    draft = "## Run this\n\n```bash\npct exec 104 -- apt-get install -y --only-upgrade sonarr=4.0.20.3014\n```\n"
    frame = sr.frame_run(T5, draft, spec, policy, services=[SONARR],
                         inventory={"vms": {}, "cts": {"104": "running"}, "names": {"104": "sonarr"}})
    assert not frame["refused"] and frame["inputs"] == [], (frame["refused"], frame["inputs"])
    assert len(frame["commands"]) == 3 and "apt-get" not in " ".join(frame["commands"])


def test_the_phases_parse_in_bash():
    for c in au.block(T5, [SONARR])[0]:
        assert subprocess.run(["bash", "-n", "-c", c], capture_output=True).returncode == 0, c
