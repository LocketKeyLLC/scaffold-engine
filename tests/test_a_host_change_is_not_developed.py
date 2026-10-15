"""§17.1431 — a step whose own text names a host-level change is not routed to the develop flow.

Live, 2026-10-08, ADD128 ("CT 120 (existing Caddy): fix its DNS and serve the panel …"), whose work is
`pct set 120 --nameserver …` + a reboot + a Caddyfile: routed to develop because its title names CT 120 and
Caddy there has a working directory; the develop delivery carries files only, and eight rounds returned none.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from app.modules import develop as dv
from app.modules import service_truth as st

FIX = pathlib.Path(__file__).parent / "fixtures"
CADDY = st.ServiceTruth(guest="120", name="caddy", unit="caddy.service", workdir="/etc/caddy")
PANEL = st.ServiceTruth(guest="111", name="control-panel", unit="control-panel.service", workdir="/opt/control-panel-backend")


def test_add128_is_not_developed():
    node = json.loads((FIX / "add128_node_2026_10_08.json").read_text())
    assert dv.host_for(node, [CADDY, PANEL]) is None


@pytest.mark.parametrize("fixture", ["add122_node_2026_10_07.json", "add123_node_2026_10_07.json",
                                     "add124_node_2026_10_08.json", "add125_node_2026_10_08.json",
                                     "add127_node_2026_10_08.json"])
def test_the_panel_feature_steps_still_are(fixture):
    node = json.loads((FIX / fixture).read_text())
    assert dv.host_for(node, [CADDY, PANEL]) is not None, fixture


# §17.1454 — the guest the title names hosts the work, or nothing does.
RADARR = st.ServiceTruth(guest="103", name="radarr", unit="radarr.service", workdir="/opt/Radarr")


def test_a_step_on_a_guest_with_no_measured_service_is_not_hosted_elsewhere():
    """Live (ADD149, 2026-10-10): no measured service on CT 120 had a directory; the name fallback picked
    Radarr (CT 103) because the text says "Radarr's 302"."""
    node = json.loads((FIX / "add149_node_2026_10_10.json").read_text())
    assert "radarr" in node["description"].lower() and node["title"].startswith("CT 120")
    assert dv.host_for(node, [PANEL, RADARR]) is None
    host = dv.host_for(node, [PANEL, RADARR, CADDY])
    assert host is None or host.guest == "120"


def test_a_validation_or_write_up_step_is_not_developed():
    """§17.1454 — T37 "Validate entire build" was developed (its text names the backend it checks)."""
    t37 = json.loads((FIX / "t37_node_2026_10_10.json").read_text())
    assert t37["title"] == "Validate entire build"
    assert dv.host_for(t37, [PANEL, CADDY, RADARR]) is None
    for title in ("Document architecture and setup", "Verify the panel answers", "Test the media request"):
        assert dv.host_for({"title": title, "description": "LXC 111: build the control-panel route"}, [PANEL]) is None


JELLYFIN = st.ServiceTruth(guest="101", name="jellyfin", unit="jellyfin.service", workdir="/var/lib/jellyfin")


def test_a_service_only_the_description_mentions_is_not_the_host():
    """§17.1456 — ADD151 (firewall on every guest) was hosted on Jellyfin because its text lists Jellyfin's ports."""
    node = json.loads((FIX / "add151_node_2026_10_10.json").read_text())
    assert "jellyfin" in node["description"].lower() and "jellyfin" not in node["title"].lower()
    assert dv.host_for(node, [PANEL, RADARR, JELLYFIN, CADDY]) is None


def test_a_step_that_configures_the_guest_is_not_a_service_build():
    """§17.1465 — ADD154 (VM 106 to DHCP) was developed in the Palworld service's directory."""
    node = json.loads((FIX / "add154_node_2026_10_11.json").read_text())
    pal = st.ServiceTruth(guest="106", name="palworld", unit="palworld.service", workdir="/home/steam/palworld", vm=True)
    assert dv.host_for(node, [pal, PANEL]) is None


# §17.1601 — Proxmox's own host administration is a host change too.
def test_a_proxmox_token_step_is_not_developed():
    """Live (c2792d4b T14, 2026-10-15): its text names `pveum user token add …`; the rule knew only pct/qm verbs, so it
    was developed as a new panel route and the token was never going to be created."""
    node = json.loads((FIX / "panel_t14_node_2026_10_15.json").read_text())
    assert dv.host_for(node, [CADDY, PANEL]) is None


def test_the_api_reader_step_that_follows_it_is_still_developed():
    node = json.loads((FIX / "panel_t15_node_2026_10_15.json").read_text())
    assert dv.host_for(node, [CADDY, PANEL]) is not None


@pytest.mark.parametrize("cmd", ["pveum user token add panel@pve panel", "pvesh create /access/users", "pvesm add dir x",
                                 "pvenode config set", "pct push 111 /tmp/a /opt/a", "pct pull 111 /a /tmp/a"])
def test_proxmox_host_commands_are_host_changes(cmd):
    assert dv._HOST_CHANGE_RE.search(cmd)


@pytest.mark.parametrize("cmd", ["pvesh get /cluster/resources", "pct exec 111 -- ls", "pct config 111"])
def test_proxmox_reads_are_not(cmd):
    assert not dv._HOST_CHANGE_RE.search(cmd)
