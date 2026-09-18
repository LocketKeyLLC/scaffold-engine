"""§17.1089/§17.1104 — fact→plan reconciliation detectors."""
from __future__ import annotations
from app.modules.assist_plan_facts import (
    blocking_candidates, satisfied_candidates, obsolete_candidates,
)

PENDING = [
    {"node_key": "ADD19", "title": "Give VM 106 a 40G local-lvm boot disk", "prompt_template": ""},
    {"node_key": "ADD59", "title": "Expand VM 106's disk to 100GB", "prompt_template": ""},
    {"node_key": "T7",   "title": "Set up recurring invoices", "prompt_template": ""},
]

def test_blocking_fact_matches_the_capability_step():
    c = blocking_candidates(["Recurring transactions are not available on Simple Start"], PENDING)
    assert c and "T7" in c[0]["node_keys"]

def test_satisfied_fact_matches_the_done_step():
    # the real fact shape: "... is now ...40G (confirmed by qm config)"
    c = satisfied_candidates(
        ["VM 106 (palworld-server) scsi0 disk is now local-lvm:vm-106-disk-0,size=40G (confirmed)"], PENDING)
    keys = {k for cand in c for k in cand["node_keys"]}
    assert "ADD19" in keys

def test_obsolete_fact_matches_the_reversed_step():
    # the real fact shape: "... deleted and recreated as 40G, replacing the previous 100G"
    c = obsolete_candidates(
        ["VM 106 scsi0 disk was deleted and recreated as a 40G volume, replacing the previous 100G disk"], PENDING)
    keys = {k for cand in c for k in cand["node_keys"]}
    assert "ADD59" in keys   # the 100GB step is what got reversed

def test_plain_status_fact_is_not_obsolete_or_blocking():
    plain = ["The host at 192.168.1.129 is reachable via SSH"]
    assert obsolete_candidates(plain, PENDING) == []
    assert blocking_candidates(plain, PENDING) == []
