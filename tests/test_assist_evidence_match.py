"""§17.1101/1102 — evidence→step matching pre-filter and duplicate clustering."""
from __future__ import annotations

from app.modules.assist_evidence_match import (
    evidence_tokens, score_candidate, shortlist_candidates, near_duplicate_clusters,
)

EVID = "root@pve:~# qm config 106 | grep scsi0\nscsi0: local-lvm:vm-106-disk-0,size=40G"
STEPS = [
    {"node_key": "ADD15", "match_text": "Set VM 106's scsi0 disk to 40G on local-lvm"},
    {"node_key": "ADD19", "match_text": "Give VM 106 a 40G local-lvm boot disk"},
    {"node_key": "ADD22", "match_text": "Give VM 106 (palworld-server) a 40G local-lvm boot disk"},
    {"node_key": "ADD38", "match_text": "Resize VM 106's scsi0 disk to 40G"},
    {"node_key": "T20", "match_text": "Install the NVIDIA driver on the Proxmox host"},
    {"node_key": "ADD16", "match_text": "Attach the Ubuntu 22.04.3 ISO to VM 110's ide2 CD-ROM"},
]


def test_tokens_extract_concrete_signals():
    t = evidence_tokens(EVID)
    assert "106" in t and "40g" in t and "scsi0" in t and "qm" in t

def test_strong_tokens_outweigh_words():
    # sharing the id 106 + 40g (strong) beats sharing a generic word
    s_disk = score_candidate(evidence_tokens(EVID), "Resize VM 106's scsi0 disk to 40G")
    s_other = score_candidate(evidence_tokens(EVID), "Install the NVIDIA driver on the host")
    assert s_disk >= 6 and s_other < 3

def test_shortlist_finds_the_disk_steps_not_the_unrelated_ones():
    top = shortlist_candidates(EVID, STEPS, top_k=5, min_score=3)
    keys = {c["node_key"] for c in top}
    assert {"ADD15", "ADD19", "ADD22", "ADD38"} <= keys
    assert "T20" not in keys and "ADD16" not in keys   # different machine/work
    assert top[0]["node_key"] == "ADD15"               # best score first

def test_near_duplicate_clusters_group_same_work_only():
    clusters = near_duplicate_clusters(STEPS)
    # the four VM-106-40G-disk steps cluster together; nothing else joins them
    disk = next((c for c in clusters if any(s["node_key"] == "ADD15" for s in c)), [])
    keys = {s["node_key"] for s in disk}
    assert {"ADD15", "ADD19", "ADD22", "ADD38"} <= keys
    assert "T20" not in keys and "ADD16" not in keys

def test_no_false_cluster_across_machines():
    steps = [
        {"node_key": "A", "match_text": "Resize VM 106 scsi0 to 40G"},
        {"node_key": "B", "match_text": "Resize VM 110 scsi0 to 40G"},
    ]
    # same size/word but DIFFERENT machine id → not the same work
    assert near_duplicate_clusters(steps) == []
