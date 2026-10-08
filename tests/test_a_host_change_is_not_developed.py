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
