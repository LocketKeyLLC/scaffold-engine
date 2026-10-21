"""§17.1742 — the planner's phrasings for work only the operator's hands can do are the operator's steps.

Live (job f9ca3ee1, the corrected re-plan: laptops with no OS yet). Measured over 797 stored steps: these 5, no other."""
from __future__ import annotations

import pytest

from app.modules import operator_steps as ops

LIVE = {
    "T1": "Ask operator to report each laptop's make/model, RAM, disk size, CPU cores. Produce a structured inventory. No action on machines yet.",
    "T8": "Step-by-step walkthrough for operator to install chosen OS on laptop 1. Operator performs physical install; engine provides instructions only.",
    "T10": "Repeat walkthrough for each remaining laptop. One node covers the pattern; operator executes per laptop.",
    "T11": "Walkthrough: connect each laptop to LAN, enable SSH, set static IP or DHCP reservation. Operator confirms each laptop's address.",
    "T12": "Walkthrough: operator adds engine's public SSH key to each laptop's authorized_keys. Engine pins each laptop address before use.",
}


@pytest.mark.smoke
@pytest.mark.parametrize("key", sorted(LIVE))
def test_the_live_steps_are_the_operators(key):
    assert ops.is_wholly_operators({"title": key, "description": LIVE[key], "tool": "LLM"})


@pytest.mark.smoke
@pytest.mark.parametrize("text", [
    "Get jellyfin's API key ON THE MACHINE -- never ask the operator for an admin key or password.",
    "Read the keys from each app's config.xml; do not ask the operator to paste them.",
    "Install k3s server on VM 110 as the control plane.",
])
def test_engine_work_stays_the_engines(text):
    assert not ops.is_wholly_operators({"title": "x", "description": text, "tool": "Shell"})
