"""§17.1657 — a call to an *arr's own API inside its guest presents the app's key (job d89078ed T3/T14: keyless → 401)."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock

from app.modules import arr_api_key as ak
from app.modules import runbook_templates as rt
from app.modules import supervised_runs as sr
from app.modules.service_truth import ServiceTruth

RADARR = ServiceTruth(guest="103", unit="radarr.service", name="radarr", ports=("7878",),
                      argv="/opt/Radarr/Radarr -nobrowser -data=/var/lib/radarr", data_dir="/var/lib/radarr")
T3_VERIFY = "pct exec 103 -- bash -c \"curl -s http://localhost:7878/api/v3/system/status | grep -o version\""


def test_a_check_gets_the_placeholder_quoted_for_its_place():
    _, (v,), _, notes = ak.present_the_arr_key([], [T3_VERIFY], [], [RADARR])
    assert 'curl -H \\"X-Api-Key: <RADARR_API_KEY>\\" -s http://localhost:7878' in v and notes
    (c,), _, _, _ = ak.present_the_arr_key(["pct exec 103 -- curl -s http://127.0.0.1:7878/api/v3/health"], [], [], [RADARR])
    assert c == 'pct exec 103 -- curl -H "X-Api-Key: <RADARR_API_KEY>" -s http://127.0.0.1:7878/api/v3/health'


def test_the_template_body_gets_the_in_guest_read():
    body = rt.render(rt.RUN_IN_CONTAINER, {"GID": "103", "STEP": "T14", "VERIFY_INSIDE": "true",
                                          "REMOTE_COMMANDS": "curl -s http://localhost:7878/api/v3/health | python3 -c 'print(1)'"})
    script = body.split("```bash\n", 1)[1].split("\n```", 1)[0]
    _, _, (f,), _ = ak.present_the_arr_key([], [], [{"path": "/tmp/in_ct_103.sh", "content": script}], [RADARR])
    line = next(ln for ln in f["content"].split("\n") if "7878/api/v3/health" in ln)
    assert line.startswith('curl -H "X-Api-Key: $(cat /var/lib/radarr/config.xml') and "<RADARR_API_KEY>" not in line
    assert subprocess.run(["bash", "-n", "-c", line], capture_output=True).returncode == 0


def test_what_is_left_alone():
    keyed = 'pct exec 103 -- curl -s -H "X-Api-Key: k" http://127.0.0.1:7878/api/v3/health'
    other_guest = "pct exec 104 -- curl -s http://127.0.0.1:7878/api/v3/health"
    not_arr = "pct exec 103 -- curl -s http://127.0.0.1:8096/System/Info/Public"
    query = "pct exec 103 -- curl -s 'http://127.0.0.1:7878/api/v3/health?apikey=k'"
    out, _, _, notes = ak.present_the_arr_key([keyed, other_guest, not_arr, query], [], [], [RADARR])
    assert out == [keyed, other_guest, not_arr, query] and notes == []


def test_the_frame_reads_the_key_in_the_guest_and_asks_nothing():
    spec = MagicMock(); spec.name = "pve-runner"; spec.headers = {}
    pol = {"allow": ["ANY"], "sudo": True, "helper": "23", "held": [], "held_logins": []}
    rb = "## Run this\n\n```bash\npct exec 103 -- systemctl is-active radarr.service\n```\n\n## Verify\n\n```bash\n" + T3_VERIFY + "\n```\n"
    f = sr.frame_run({"node_key": "T3", "title": "Verify Radarr API version", "tool": "Shell"}, rb, spec, pol,
                     services=[RADARR], inventory={"vms": {}, "cts": {"103": "running"}, "names": {"103": "radarr"}})
    assert f["inputs"] == [] and "sed -n" in f["verify"][0] and "<RADARR_API_KEY>" not in f["verify"][0], f["verify"]
