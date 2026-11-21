"""§17.1966 — a split's children are the parent's work in parts: they keep its tool and its place, and the first
stands on what still stands.

Live (job 7cc2a7ac, 2026-11-20): T16 "Proxmox host: test the control panel game-server controls through its API"
(tool Shell, guest CT 111 in its body) was split into ADD1-ADD8 and then ADD5 re-split into ADD9-ADD15:

1. every child was inserted with tool `LLM` (`children_from` hard-coded it) -- a control test became a text answer,
   and ADD5/ADD6/ADD7 had to be edited back to Shell by hand;
2. ADD1-ADD4 were skipped at 23:22 and the re-split at 23:35 chained ADD9 onto ADD4 -- a step that ran nothing;
3. every child titled "Proxmox host: …" carried "On container 111." from T16's guest.

The fixture is the live shape with made-up text (no address of the home).
"""
from __future__ import annotations

import inspect

import pytest

from app.modules import execution_agent as ea
from app.modules import step_decomposition as sd

#: the plan as the pause reads it, at the moment ADD5 was re-split (23:35): T15 and ADD1-ADD4 skipped
PLAN = [
    {"node_key": "T6", "status": "done", "depends_on": ["T5"]},
    {"node_key": "T7", "status": "done", "depends_on": ["T5"]},
    {"node_key": "T15", "status": "skipped", "depends_on": ["T6", "T7"]},
    {"node_key": "ADD1", "status": "skipped", "depends_on": ["T15"]},
    {"node_key": "ADD2", "status": "skipped", "depends_on": ["ADD1"]},
    {"node_key": "ADD3", "status": "skipped", "depends_on": ["ADD2"]},
    {"node_key": "ADD4", "status": "skipped", "depends_on": ["ADD3"]},
    {"node_key": "ADD5", "status": "pending", "depends_on": ["ADD4"]},
    {"node_key": "ADD8", "status": "pending", "depends_on": ["ADD7"]},
]
ADD5 = {"node_key": "ADD5", "tool": "Shell", "depends_on": ["ADD4"],
        "title": "Proxmox host: exercise the game server start/stop control and confirm the effect",
        "description": "On the Proxmox host shell. Call the panel's start and stop routes for the game server in "
                       "CT 111 and read its state back after each."}
STEPS = [
    {"title": "Proxmox host: read the game server's state before the test",
     "description": "Read the server's current state through the panel's status route.",
     "check": "curl -s http://panel.lab.test:3001/api/game/status"},
    {"title": "Proxmox host: stop the game server through the panel and read its state",
     "description": "Call the stop route, then read the state back.",
     "check": "curl -s http://panel.lab.test:3001/api/game/status"},
    {"title": "Read the stop in the panel backend's log",
     "description": "Read the last lines of the backend's journal.",
     "check": "journalctl -u panel-backend -n 5"},
]


def test_a_skipped_dependency_is_replaced_by_what_it_stood_on():
    """The live chain: ADD4 -> ADD3 -> ADD2 -> ADD1 -> T15 all skipped; what stands is T6 and T7."""
    assert sd.standing_deps(PLAN, ["ADD4"]) == ["T6", "T7"]


def test_a_dependency_that_stands_is_kept_as_it_was():
    """The correct misses: a done or pending step, a step the plan does not carry, and a skipped row read
    without its `depends_on` (nothing to walk to) are all kept, in order."""
    assert sd.standing_deps(PLAN, ["T6", "ADD8"]) == ["T6", "ADD8"]
    assert sd.standing_deps(PLAN, ["T99"]) == ["T99"]
    assert sd.standing_deps([{"node_key": "ADD4", "status": "skipped"}], ["ADD4"]) == ["ADD4"]
    assert sd.standing_deps(PLAN, ["T6", "ADD4"]) == ["T6", "T7"], "said once is said"


def test_the_children_keep_the_parents_tool():
    kids = sd.children_from(STEPS, parent_key="ADD5", parent_deps=["T6"], keys=["ADD9", "ADD10", "ADD11"],
                            tool="Shell")
    assert [k["tool"] for k in kids] == ["Shell", "Shell", "Shell"]
    assert all(k["tool"] == "LLM" for k in sd.children_from(STEPS, parent_key="ADD5", parent_deps=[],
                                                             keys=["ADD9", "ADD10", "ADD11"])), "no tool, as before"


def test_a_host_placed_child_gets_no_guest_place():
    kids = sd.children_from(STEPS, parent_key="ADD5", parent_deps=[], keys=["ADD9", "ADD10", "ADD11"],
                            machine="container 111")
    assert "On container 111." not in kids[0]["description"], "its title places it on the Proxmox host"
    assert "On container 111." not in kids[1]["description"]
    assert "On container 111." in kids[2]["description"], "a child with no place of its own still gets one"


def test_the_pause_reads_the_field_standing_deps_needs():
    """The lane: `standing_deps` walks `depends_on`, so the plan the pause hands `split_step` must carry it."""
    src = inspect.getsource(ea._pause_for_decision)
    at = src.index("ORDER BY execution_order")
    assert "depends_on FROM dag_nodes WHERE job_id = :j" in src[at - 400:at], src[at - 400:at]


@pytest.mark.asyncio
async def test_the_live_resplit_inserts_shell_children_on_t6_t7_with_no_guest_place(monkeypatch):
    from app.modules import node_editor
    import app.database as _db
    inserted: list[dict] = []

    class _S:
        async def __aenter__(self): return object()
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(_db, "async_session", lambda: _S())

    async def fake_propose(node, brief, environment, upstream="", reason=""):
        return STEPS
    monkeypatch.setattr(sd, "propose_split", fake_propose)

    async def fake_insert(job_id, spec, *, db, edited_by):
        inserted.append(spec); return {"status": "ok", "node_key": spec["node_key"]}

    async def fake_edit(job_id, key, fields, *, db, cascade, edited_by):
        return {"status": "ok"}

    async def fake_list(job_id, db):
        return {"nodes": [{"node_key": n["node_key"]} for n in PLAN]}

    async def fake_reorder(job_id, order, *, db, edited_by):
        return {"status": "ok"}
    monkeypatch.setattr(node_editor, "insert_node", fake_insert)
    monkeypatch.setattr(node_editor, "edit_node", fake_edit)
    monkeypatch.setattr(node_editor, "list_nodes", fake_list)
    monkeypatch.setattr(node_editor, "reorder_nodes", fake_reorder)

    did = await sd.split_step("job", dict(ADD5), PLAN, {}, {}, reason="content cut: 4034 chars",
                              machine="container 111")
    assert did and [s["node_key"] for s in inserted] == ["ADD9", "ADD10", "ADD11"]
    assert inserted[0]["depends_on"] == ["T6", "T7"], "never the skipped ADD4"
    assert [s["tool"] for s in inserted] == ["Shell"] * 3, "the parent's tool"
    assert not any("On container 111." in s["description"] for s in inserted), "a host-placed parent's guest is not theirs"
