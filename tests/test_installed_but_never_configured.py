"""§17.1219 — a service installed and never configured is not a plan.

    "at no point did the engine assist in setting up where and how to retrieve
     movies and media for the homelab."

The plan had installed the entire stack for exactly that — Prowlarr, Radarr,
Sonarr, a download client — and not one step added an indexer, connected Radarr
to Prowlarr, pointed either at the download client, set a root folder or chose a
quality. Four services whose whole purpose is their configuration, left at
defaults. §17.686 already tells the planner to emit a node for each
install/configure/integrate/verify outcome; it simply did not.
"""
from __future__ import annotations

from app.modules import plan_coverage as pc

# the shape of the operator's real plan
PLAN = [
    {"node_key": "T9", "title": "Create Jellyfin LXC"},
    {"node_key": "T10", "title": "Install Jellyfin"},
    {"node_key": "T12", "title": "Configure Jellyfin libraries"},
    {"node_key": "T13", "title": "Create Prowlarr LXC"},
    {"node_key": "T14", "title": "Install Prowlarr"},
    {"node_key": "T15", "title": "Create Radarr LXC"},
    {"node_key": "T16", "title": "Install Radarr"},
    {"node_key": "T19", "title": "Create download client LXC"},
    {"node_key": "T20", "title": "Install download client"},
]


def test_the_media_stack_gap_is_found():
    subs = {g["subject"] for g in pc.uncovered(PLAN)}
    assert {"prowlarr", "radarr"} <= subs, subs


def test_a_service_that_IS_configured_is_not_a_finding():
    """Jellyfin is installed AND configured (T12), so it must not appear."""
    assert "jellyfin" not in {g["subject"] for g in pc.uncovered(PLAN)}


def test_configuring_later_closes_the_gap():
    fixed = PLAN + [
        {"node_key": "N1", "title": "Add indexers to Prowlarr"},
        {"node_key": "N2", "title": "Connect Radarr to Prowlarr and the download client"},
    ]
    subs = {g["subject"] for g in pc.uncovered(fixed)}
    assert "prowlarr" not in subs and "radarr" not in subs, subs


def test_primitives_are_never_a_finding():
    """Installing curl is not an unconfigured service."""
    assert pc.uncovered([{"node_key": "X", "title": "Install curl and ca-certificates"}]) == []


def test_bookkeeping_titles_are_skipped():
    """Measured against the real plan, an untightened check named "step" and
    "host" as services — a finding nobody can act on trains the reader to skip
    the whole list."""
    assert pc.uncovered([{"node_key": "ADD6", "title": "add a step for this"}]) == []
    for noise in ("step", "host", "ssh", "hostpci0", "helper", "dataset"):
        assert noise in pc._NOT_A_SERVICE, noise


def test_each_service_is_reported_once():
    dupes = PLAN + [{"node_key": "T21", "title": "Install Radarr again"}]
    keys = [g["subject"] for g in pc.uncovered(dupes)]
    assert len(keys) == len(set(keys))


def test_the_note_names_the_service_and_asks_rather_than_defaults():
    note = pc.retry_note(pc.uncovered(PLAN))
    assert "prowlarr" in note and "radarr" in note
    assert "`decision` node" in note and "plain words" in note, \
        "where the setting is taste, it must ASK — §17.1219"
    assert "left at its defaults is" in note, "say what the cost to the operator is"


def test_no_gaps_no_note():
    assert pc.retry_note([]) == ""
