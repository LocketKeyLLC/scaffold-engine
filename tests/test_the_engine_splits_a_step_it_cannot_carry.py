"""§17.1330 — a step the engine cannot carry as one block is split into steps it can.

Live, 2026-10-03: ADD100 "Rebuild the control panel to do what was chosen in ADD99"
asks for three capabilities across a backend, a frontend and a service. Its draws
came back 12,614 characters, cut mid-line; §17.1312 refused them, correctly, and the
frame offered `myself | skip` — a dead end in front of the operator's own decision.
It had happened quietly before: T33 "Build control panel backend" and T34 "Build
control panel frontend" are CodeGen nodes recorded done with 553 and 154 bytes."""
from __future__ import annotations

import json
import pathlib

import pytest

from app.modules import step_decomposition as sd

FX = pathlib.Path(__file__).parent / "fixtures"
CUT = json.loads((FX / "add100_frame_cut_refused_2026_10_03.json").read_text(encoding="utf-8"))
ADD100 = {"node_key": "ADD100", "title": "Rebuild the control panel to do what was chosen in ADD99",
          "description": "Rework the control-panel backend and frontend in LXC 111 so it does the things chosen in ADD99.",
          "depends_on": ["ADD99", "ADD110", "ADD111"]}
PLAN = [{"node_key": "ADD99", "title": "Decide what the panel does", "status": "done"},
        {"node_key": "ADD100", "title": ADD100["title"], "status": "pending"},
        {"node_key": "ADD120", "title": "Forward UDP 8211", "status": "done"}]
STEPS = [
    {"title": "Install the backend's packages in container 111", "description": "apt and npm for the panel backend.", "check": "dpkg -l nodejs"},
    {"title": "Write the panel backend's Palworld settings endpoints", "description": "GET and POST the settings file.", "check": "curl -s localhost:3001/api/palworld/settings"},
    {"title": "Write the panel backend's media-request endpoint", "description": "POST a title to the media server.", "check": "curl -s localhost:3001/api/media/health"},
    {"title": "Build the panel frontend", "description": "the three controls, served statically.", "check": "ls /opt/control-panel-ui/dist/index.html"},
]


def test_the_live_cut_refusal_is_what_too_large_reads():
    assert any("content cut" in r["why"] for r in CUT["refused"])
    assert "run" not in {o.get("id") for o in CUT.get("options") or []}, "the live frame offered no Run"
    why = sd.too_large(CUT)
    assert "too large for one draw" in why and len(why) <= 300
    assert sd.too_large({"refused": [{"why": "`192.168.1.130` appears in nothing the engine holds"}]}) == "", \
        "a wrong VALUE is not a step that does not fit"
    assert sd.too_large({"refused": []}) == "" and sd.too_large(None) == ""


def test_the_children_are_chained_stamped_and_carry_their_check():
    kids = sd.children_from(STEPS, parent_key="ADD100", parent_deps=["ADD99", "ADD110"], keys=["ADD121", "ADD122", "ADD123", "ADD124"], machine="container 111")
    assert [k["node_key"] for k in kids] == ["ADD121", "ADD122", "ADD123", "ADD124"]
    assert kids[0]["depends_on"] == ["ADD99", "ADD110"], "the first child inherits the step's own dependencies"
    assert [k["depends_on"] for k in kids[1:]] == [["ADD121"], ["ADD122"], ["ADD123"]], "then one after another"
    assert all(k["tool"] == "LLM" for k in kids)
    assert "Done when `dpkg -l nodejs` shows it." in kids[0]["description"]
    assert all("[Engine split of ADD100 — " in k["description"] for k in kids)
    assert "4 of 4]" in kids[3]["description"]
    # the machine is added only when the child does not already name it
    assert "On container 111." not in kids[0]["description"], "its title already says so"
    assert "On container 111." in kids[1]["description"]
    assert sd.already_split([{"node_key": "ADD121", "description": kids[0]["description"]}], "ADD100") == ["ADD121"]
    assert sd.already_split(PLAN, "ADD100") == []


def test_the_next_keys_follow_the_plans_own_numbering():
    assert sd._next_keys(PLAN, 3) == ["ADD121", "ADD122", "ADD123"]
    assert sd._next_keys([{"node_key": "T1"}, {"node_key": "T2"}], 2) == ["ADD1", "ADD2"]


@pytest.mark.asyncio
async def test_a_split_inserts_chains_reorders_and_makes_the_step_wait(monkeypatch):
    from app.modules import node_editor
    import app.database as _db
    calls = []

    class _S:
        async def __aenter__(self): return object()
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(_db, "async_session", lambda: _S())

    async def fake_propose(node, brief, environment, upstream="", reason=""):
        calls.append(("propose", node["node_key"], reason[:30])); return STEPS
    monkeypatch.setattr(sd, "propose_split", fake_propose)

    async def fake_insert(job_id, spec, *, db, edited_by):
        calls.append(("insert", spec["node_key"], tuple(spec["depends_on"]))); return {"status": "ok", "node_key": spec["node_key"]}

    async def fake_edit(job_id, key, fields, *, db, cascade, edited_by):
        calls.append(("edit", key, tuple(fields["depends_on"]), cascade)); return {"status": "ok"}

    async def fake_list(job_id, db):
        return {"nodes": [{"node_key": "ADD99"}, {"node_key": "ADD100"}, {"node_key": "ADD120"},
                          {"node_key": "ADD121"}, {"node_key": "ADD122"}, {"node_key": "ADD123"}, {"node_key": "ADD124"}]}

    async def fake_reorder(job_id, order, *, db, edited_by):
        calls.append(("reorder", tuple(order))); return {"status": "ok"}
    monkeypatch.setattr(node_editor, "insert_node", fake_insert)
    monkeypatch.setattr(node_editor, "edit_node", fake_edit)
    monkeypatch.setattr(node_editor, "list_nodes", fake_list)
    monkeypatch.setattr(node_editor, "reorder_nodes", fake_reorder)

    node = dict(ADD100)
    did = await sd.split_step("job", node, PLAN, {"description": "home lab"}, {"profile": "root@pve"},
                              upstream="ADD99 decided: three things", reason="content cut: 12614 chars", machine="container 111")
    inserted = [c[1] for c in calls if c[0] == "insert"]
    assert inserted == ["ADD121", "ADD122", "ADD123", "ADD124"]
    assert [c[2] for c in calls if c[0] == "insert"][0] == ("ADD99", "ADD110", "ADD111")
    assert ("edit", "ADD100", ("ADD99", "ADD110", "ADD111", "ADD124"), False) in calls, calls
    order = next(c[1] for c in calls if c[0] == "reorder")
    assert order.index("ADD124") < order.index("ADD100"), "the children run before the step they came from"
    assert order.index("ADD121") == order.index("ADD100") - 4
    assert node["depends_on"][-1] == "ADD124"
    assert did[0].startswith("split ADD100 into 4 steps it can carry: ADD121")
    assert did[-1] == "ADD100 now waits for ADD124", "the pause restarts on this line (§17.1309/1313)"


@pytest.mark.asyncio
async def test_a_step_is_split_once_and_an_unusable_proposal_changes_nothing(monkeypatch):
    from app.modules import node_editor
    import app.database as _db
    touched = []

    class _S:
        async def __aenter__(self): return object()
        async def __aexit__(self, *a): return False
    monkeypatch.setattr(_db, "async_session", lambda: _S())

    async def boom(*a, **k):
        touched.append(1); return {"status": "ok"}
    monkeypatch.setattr(node_editor, "insert_node", boom)
    monkeypatch.setattr(node_editor, "edit_node", boom)

    kids = sd.children_from(STEPS[:2], parent_key="ADD100", parent_deps=[], keys=["ADD121", "ADD122"])
    plan_with_children = PLAN + [{"node_key": k["node_key"], "description": k["description"], "status": "pending"} for k in kids]

    async def never(*a, **k):
        raise AssertionError("already split: nothing to propose")
    monkeypatch.setattr(sd, "propose_split", never)
    assert await sd.split_step("job", dict(ADD100), plan_with_children, {}, {}) == []

    async def one_step(*a, **k):
        return [STEPS[0]]
    monkeypatch.setattr(sd, "propose_split", one_step)
    assert await sd.split_step("job", dict(ADD100), PLAN, {}, {}) == []

    async def nothing(*a, **k):
        return []
    monkeypatch.setattr(sd, "propose_split", nothing)
    assert await sd.split_step("job", dict(ADD100), PLAN, {}, {}) == []
    assert touched == [], "nothing was written for any of the three"


@pytest.mark.asyncio
async def test_the_draw_is_asked_for_blocks_and_fails_soft(monkeypatch):
    import app.model_router as mr
    seen = {}

    async def fake_tool_call(*, messages, tools, **kw):
        seen["prompt"] = messages[0]["content"]; seen["tool"] = tools[0].name
        call = type("C", (), {"name": tools[0].name, "arguments": {"steps": STEPS}})()
        return type("R", (), {"success": True, "tool_calls": [call], "text": ""})()
    monkeypatch.setattr(mr, "tool_call", fake_tool_call)
    out = await sd.propose_split(ADD100, {"description": "home lab"}, {"facts": ["CT 111 runs the panel"], "substitutions": {"PANEL_IP": "192.168.1.25"}},
                                 upstream="ADD99: three things", reason="content cut")
    assert len(out) == 4 and seen["tool"] == "record_step_split"
    p = seen["prompt"]
    assert "one approved block of shell commands per step" in p and "between 2 and 8 steps" in p
    assert "ADD99: three things" in p and "CT 111 runs the panel" in p and "PANEL_IP = 192.168.1.25" in p
    assert "Do not invent addresses, ports, unit names or credentials" in p

    async def raises(*a, **k):
        raise RuntimeError("model down")
    monkeypatch.setattr(mr, "tool_call", raises)
    assert await sd.propose_split(ADD100, {}, {}) == []


def test_the_pause_splits_before_it_parks_and_only_once_per_descent():
    from app.modules import execution_agent as ea
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def _pause_for_decision(")
    body = src[i:src.index("\nasync def ", i + 10)]
    assert body.count("_sd.split_step(") == 1 and body.count("_sd.too_large(frame)") == 1
    assert body.index("_sd.split_step(") < body.index('logger.warning("supervised_run_parked'), "split before parking"
    assert body.index("supervised_runs.already_met(") < body.index("_sd.too_large(frame)"), "a step already done is not split"
    assert "_pause_for_decision(job_id, _depth + 1)" in body and "if _depth < 6:" in body
