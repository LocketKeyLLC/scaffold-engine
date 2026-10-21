"""§17.1741 — a stored secret goes only to a machine the engine knows.

Live (job f9ca3ee1 T2, 2026-10-21; addresses made up here): after a LAN scan and before the operator confirmed which
devices are laptops, the draft ssh'd with `SSHPASS="$MASS_PASSWORD"` into four scanned addresses, one of them the
operator's own workstation."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.modules import supervised_runs as sr

ENV = {"facts": ["host pve at 192.0.2.156", "VM 110 ai-vm at 192.0.2.129"],
       "substitutions": {"PALWORLD_IP": "192.0.2.106"},
       "system_state": {"host": {"kind": "host", "attrs": {"ip": "192.0.2.156"}}}}
POLICY = {"allow": ["ANY"], "sudo": True, "helper": "24", "secrets": ["MASS_PASSWORD"], "held": ["SONARR_API_KEY"]}
SERVICES = [SimpleNamespace(app="sonarr", address="192.0.2.23")]
LIVE = ["apt-get install -y sshpass",
        "nmap -p 22 --open 192.0.2.43 192.0.2.59 192.0.2.199 192.0.2.204",
        "SSHPASS=\"$MASS_PASSWORD\" sshpass -e ssh -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "
        "aedefruscio@192.0.2.43 'hostname; uname -a'"]


@pytest.mark.smoke
def test_the_live_spray_is_refused():
    out = sr.a_secret_sent_to_an_unconfirmed_machine(LIVE, POLICY, ENV, SERVICES)
    assert len(out) == 1 and "192.0.2.43" in out[0]["why"]
    assert any(m in out[0]["why"] for m in sr._SHAPE_REFUSALS), "a refusal no redraft recognises is a dead end"


@pytest.mark.smoke
@pytest.mark.parametrize("cmd", [
    'SSHPASS="$MASS_PASSWORD" sshpass -e ssh-copy-id -o StrictHostKeyChecking=accept-new aedefruscio@192.0.2.129',
    'curl -s -H "X-Api-Key: $SONARR_API_KEY" http://192.0.2.23:8989/api/v3/indexer',   # a measured service
    "pct exec 105 -- sh -c 'tr -d \"\\n\" | curl -s --data-urlencode password@- http://127.0.0.1:8080/api/v2/auth/login' <<< \"$MASS_PASSWORD\"",
    "nmap -p 22 --open -sV 192.0.2.43",                                                   # no secret: identify freely
])
def test_known_machines_loopback_and_secretless_lines_pass(cmd):
    assert sr.a_secret_sent_to_an_unconfirmed_machine([cmd], POLICY, ENV, SERVICES) == []


@pytest.mark.smoke
def test_a_pinned_address_becomes_known():
    pinned = {**ENV, "substitutions": {**ENV["substitutions"], "LAPTOP1_IP": "192.0.2.59"}}
    line = ['SSHPASS="$MASS_PASSWORD" sshpass -e ssh aedefruscio@192.0.2.59 hostname']
    assert sr.a_secret_sent_to_an_unconfirmed_machine(line, POLICY, ENV, SERVICES)
    assert sr.a_secret_sent_to_an_unconfirmed_machine(line, POLICY, pinned, SERVICES) == []


def test_frame_run_refuses_the_live_block():
    rb = "## Run this\n\n```bash\n" + "\n".join(LIVE) + "\n```\n\n## Verify\n\n- `nmap -p 22 --open 192.0.2.43`\n"
    node = {"node_key": "T2", "title": "Identify laptop candidates", "description": "Flag likely laptops."}
    frame = sr.frame_run(node, rb, SimpleNamespace(name="pve-runner", headers={}), POLICY, env=ENV, services=SERVICES)
    assert [r for r in frame["refused"] if "has not been confirmed as one of this lab's machines" in r["why"]]
    assert frame["suggested"] != "run"
