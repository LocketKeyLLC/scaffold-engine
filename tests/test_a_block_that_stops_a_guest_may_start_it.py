"""§17.1610 — the precondition rule follows the state the block itself leaves each guest in.

Live (job 0fb4ea7f T4 "Expand ai-vm RAM", 2026-10-15): `qm shutdown 110` → `qm set 110 --memory …` → `qm start 110` was
refused at the start: "VM 110 is ALREADY running" — the listing's state, not the block's.
"""
from __future__ import annotations

import asyncio

from app.modules.runbook_preconditions import unmet

SPEC = type("S", (), {"name": "pve-runner", "headers": {}})()
INV = {"cts": {"111": "running"}, "vms": {"110": "running"}, "names": {"110": "ai-vm", "111": "control-panel"},
       "disks": {}, "isos": []}


def _unmet(cmds):
    return asyncio.run(unmet(cmds, SPEC, plan=[], files=[], node={"title": "x"}, inventory=INV))


def test_the_live_ram_change_is_not_refused():
    cmds = ["qm status 110", "qm shutdown 110", "qm status 110", "qm set 110 --memory 49152", "qm start 110"]
    assert [o["why"][:60] for o in _unmet(cmds)] == []


def test_a_start_with_no_stop_before_it_is_still_refused():
    out = _unmet(["qm start 110"])
    assert len(out) == 1 and "is ALREADY running" in out[0]["why"]


def test_work_inside_a_guest_the_block_just_stopped_is_refused():
    out = _unmet(["pct start 111", "pct stop 111", "pct exec 111 -- ls"])
    assert any("is stopped" in o["why"] for o in out), [o["why"][:80] for o in out]

