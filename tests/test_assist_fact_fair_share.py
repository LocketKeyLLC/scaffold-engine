"""§17.939 — the fact ledger must not let one subject evict all the others.

`environment.facts` is a flat list under a global cap (`assist_facts_max`,
default 40) and eviction was oldest-first. §17.920 already carved out
corrections; nothing protected TOPIC diversity.

Live (session 613dd1df): the operator spent days on VM 110. Forty VM-106 /
VM-110 facts filled the ledger and EVERY media-stack fact — the Radarr,
Prowlarr and Sonarr addresses, the container ids — was evicted. Returning to
"Integrate media stack services" a week later, the engine had no context for
the step at all; its addresses survived only because they happened to still be
inside the transcript window, which is luck, not design.
"""
from collections import Counter

import pytest

from app.modules.assist_environment import _fact_subject, _fair_share_keep


# ── bucketing ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("fact,subject", [
    ("VM 106 (palworld-server) was recreated with 8192MB.", "vm:106"),
    ("Inside VM 110, the NVIDIA driver is installed.", "vm:110"),
    ("LXC container 103 runs Radarr.", "ct:103"),
    ("CT 107 hosts qBittorrent.", "ct:107"),
    ("Radarr is reachable at 192.168.1.22:7878.", "host:192.168.1.22"),
    ("The Proxmox host uses a ZFS mirror named oasis.", "general"),
    ("", "general"),
])
def test_facts_bucket_by_subject(fact, subject):
    assert _fact_subject(fact) == subject


def test_resource_id_beats_a_bare_ip():
    """Most specific wins: a fact naming a VM is about that VM even when it
    also mentions an address."""
    assert _fact_subject("VM 110 is reachable at 192.168.1.99.") == "vm:110"


# ── the eviction ──────────────────────────────────────────────────────────


def _live_shape():
    """The real ledger shape: media stack learned FIRST (2026-08-31), then days
    of VM work piled on top. Oldest-first eviction kills exactly the facts the
    operator needs when they return to the older step."""
    return (["LXC container 103 runs Radarr at 192.168.1.22:7878.",
             "CT 104 runs Prowlarr at 192.168.1.21:9696.",
             "CT 105 runs Sonarr at 192.168.1.23:8989."]
            + [f"VM 106 fact {i}" for i in range(15)]
            + [f"VM 110 fact {i}" for i in range(20)])


def test_the_live_regression_every_other_subject_was_evicted():
    """Pin the OLD behaviour so the fix cannot silently regress: newest-first
    collapses a five-subject ledger to one."""
    facts = _live_shape()
    old_way = facts[-20:]                      # what the previous trim did
    assert len({_fact_subject(f) for f in old_way}) == 1
    assert not [f for f in old_way if "Radarr" in f or "Prowlarr" in f]


def test_fair_share_keeps_every_subject_alive():
    facts = _live_shape()
    kept = _fair_share_keep(facts, 20)
    assert len(kept) == 20
    # all five subjects survive, and the thin ones keep their only fact
    assert len({_fact_subject(f) for f in kept}) == 5
    for svc in ("Radarr", "Prowlarr", "Sonarr"):
        assert any(svc in f for f in kept), f"{svc} context was evicted again"


def test_the_budget_is_taken_from_the_biggest_bucket():
    """A subject with twenty facts is what should shrink, not the one with
    one."""
    facts = _live_shape()
    c = Counter(_fact_subject(f) for f in _fair_share_keep(facts, 20))
    assert c["ct:103"] == 1 and c["ct:104"] == 1 and c["ct:105"] == 1
    assert c["vm:110"] > 5 and c["vm:106"] > 5


def test_original_order_is_preserved():
    """Callers render the ledger as a list; reordering it would reshuffle the
    injected block for no reason."""
    facts = _live_shape()
    kept = _fair_share_keep(facts, 20)
    assert kept == [f for f in facts if f in kept]


def test_under_the_cap_nothing_is_dropped():
    facts = _live_shape()
    assert _fair_share_keep(facts, 500) == facts


def test_zero_room_keeps_nothing():
    assert _fair_share_keep(_live_shape(), 0) == []


def test_single_subject_still_trims_newest_last():
    """With one subject there is no fairness to apply — it must degrade to the
    plain oldest-first trim."""
    facts = [f"VM 110 fact {i}" for i in range(10)]
    assert _fair_share_keep(facts, 4) == facts[-4:]


def test_eviction_is_deterministic():
    """Bucket iteration order must not decide what the operator keeps."""
    facts = _live_shape()
    runs = {tuple(_fair_share_keep(list(facts), 20)) for _ in range(5)}
    assert len(runs) == 1
