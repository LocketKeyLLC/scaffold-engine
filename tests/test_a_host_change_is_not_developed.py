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
