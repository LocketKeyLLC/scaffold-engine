"""§17.1329 — a step whose body is in-guest work is not host-side, and a skip stops
being coverage once the work it deferred to is voided.

Live, 2026-10-03: the reopened ADD66 "Start VM 106 and bring the PalWorld service up
on UDP 8211" was routed host-side by its first two words, drafted on the model path,
invented the unit `palworld-server` and never started anything. And ADD47/54/66/85 —
every step that started the server — stood `skipped` since September as duplicates of
work the OS reinstall had just voided, so after T24 nothing would have brought the
service up."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.modules import machine_truth as mt
from app.modules import runbook_templates as rt

AGENT = mt.GuestTruth(gid="106", kind="vm", status="running", agent=True)
ADD66 = {"node_key": "ADD66", "title": "Start VM 106 and bring the PalWorld service up on UDP 8211",
         "description": "Start VM 106 (palworld-server) on the Proxmox host, then confirm inside the guest that "
                        "the PalWorld service is enabled, active, and listening on UDP 8211."}
#: genuinely host-side: nothing points inside the guest
ADD94 = {"node_key": "ADD94", "title": "Re-attach VM 106's detached disk and grow it to 100G", "description": "qm set / qm resize on the host."}
ADD9 = {"node_key": "ADD9", "title": "Start VM 106 (palworld-server)", "description": "Start the VM on the Proxmox host."}


def test_a_start_that_also_names_in_guest_work_takes_the_guest_template():
    assert rt.host_side_only(ADD66) is False
    assert rt.select_template(ADD66, AGENT) is rt.RUN_IN_VM_VIA_AGENT
    assert rt.intent_of(ADD66) == "guest_work"


def test_host_side_work_stays_host_side():
    for node in (ADD94, ADD9, {"node_key": "X", "title": "Set VM 100 boot order to ide2", "description": ""},
                 {"node_key": "Y", "title": "Resize VM 106's disk to 100G", "description": ""},
                 {"node_key": "Z", "title": "Configure VM 100 for GPU passthrough", "description": "qm set --hostpci0"}):
        assert rt.host_side_only(node) is True, node["title"]
        assert rt.select_template(node, AGENT) is None, node["title"]
        assert rt.intent_of(node) is None, node["title"]


def test_the_steam_template_still_wins_for_a_game_install():
    T23 = {"node_key": "T23", "title": "Install PalWorld server", "description": ""}
    assert rt.select_template(T23, AGENT) is rt.INSTALL_STEAM_SERVER


# ───── a skip is not coverage once the work it deferred to is voided

def _at(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


PLAN = [
    {"node_key": "T22", "title": "Create PalWorld VM", "status": "done", "completed_at": _at("2026-08-31T22:56:51")},
    {"node_key": "T23", "title": "Install PalWorld server", "status": "done", "completed_at": _at("2026-09-04T22:02:57")},
    {"node_key": "T24", "title": "Configure PalWorld service", "status": "done", "completed_at": _at("2026-09-04T22:05:55")},
    {"node_key": "ADD47", "title": "Create, enable and start the palworld.service unit in VM 106", "status": "skipped", "completed_at": _at("2026-09-12T10:00:00")},
    {"node_key": "ADD66", "title": "Start VM 106 and bring the PalWorld service up on UDP 8211", "status": "skipped", "completed_at": _at("2026-09-20T10:00:00")},
    {"node_key": "ADD85", "title": "Create, enable, and start palworld.service", "status": "skipped", "completed_at": _at("2026-09-30T00:52:27")},
    {"node_key": "ADD81", "title": "Expand VM 106 (palworld-server) disk to 100GB", "status": "skipped", "completed_at": _at("2026-09-30T00:52:27")},
    {"node_key": "ADD117", "title": "Install Ubuntu 22.04 on VM 106 unattended (cloud image + cloud-init)", "status": "done", "completed_at": _at("2026-10-03T05:23:28")},
    {"node_key": "ADD122", "title": "Install the guest agent in VM 106", "status": "skipped", "completed_at": _at("2026-10-03T08:00:00")},
]


def test_the_live_skipped_start_steps_are_reported_and_the_host_side_skip_is_not():
    stale = mt.skips_a_reinstall_left_uncovered(PLAN, "106", "palworld-server")
    keys = [s["node_key"] for s in stale]
    assert keys == ["ADD47", "ADD66", "ADD85"], keys
    assert "ADD81" not in keys, "a skipped DISK step is host-side work the reinstall did not void"
    assert "ADD122" not in keys, "skipped AFTER the reinstall: a current judgement, left alone"
    assert mt.skips_a_reinstall_left_uncovered([n for n in PLAN if n["node_key"] != "ADD117"], "106", "palworld-server") == [], "no reinstall, no staleness"
    assert mt.skips_a_reinstall_left_uncovered(PLAN, "110", "ai-vm") == []


@pytest.mark.asyncio
async def test_the_reinstall_reports_the_stale_skips_as_a_fact_and_reopens_none_of_them(monkeypatch):
    from app.modules import node_editor
    import app.database as _db
    import app.modules.assist_environment as _ae
    calls = []

    class _R:
        def __init__(self, rows): self._rows = rows
        def mappings(self): return self
        def all(self): return self._rows
        def first(self): return self._rows[0] if self._rows else None

    class _S:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, q, params=None):
            return _R([{"id": "sid-1", "metadata": {}}] if "assist_sessions" in str(q) else PLAN)
    monkeypatch.setattr(_db, "async_session", lambda: _S())

    async def fake_reset(job_id, key, *, db, cascade, edited_by):
        calls.append(("reset", key)); return {"status": "ok"}

    async def fake_facts(*, session_id, facts, db):
        calls.append(("facts", tuple(facts)))
    monkeypatch.setattr(node_editor, "reset_node", fake_reset)
    monkeypatch.setattr(_ae, "set_environment", fake_facts)
    did = await mt.after_step_done("job", {"node_key": "ADD117", "title": PLAN[7]["title"]})
    assert [c[1] for c in calls if c[0] == "reset"] == ["T23", "T24"], "only the DONE work is reopened"
    facts = next(c[1] for c in calls if c[0] == "facts")
    assert len(facts) == 2
    assert "is reopened -- a fresh OS holds none of it" in facts[0]
    assert "SKIPPED as duplicates" in facts[1] and "ADD47" in facts[1] and "ADD66" in facts[1] and "ADD85" in facts[1]
    assert "not all of them" in facts[1], "four skipped duplicates of one goal must not become four steps"
    assert any(d.startswith("skips a reinstall left uncovered:") for d in did), did
