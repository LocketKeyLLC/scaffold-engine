"""§17.1989 — a parked frame follows its step: when the step's text, tool or dependencies change, or the step settles,
the question parked on it is withdrawn (and written down), the job re-derives, and a late answer to it is refused.

Live (job 69fe61c4, 2026-11-21): T4 "Vendor charting asset" was parked as a Shell frame (`pct exec <ENGINE_GUEST_ID> …`
on pve-runner). POST /jobs/{id}/complete-plan (§17.1985, `engine_plan.apply_to_job`) made T4 a CodeGen step with new
text; T4 ran and was `done` -- and the job, blocked on T8, still carried T4's Shell frame in
`metadata.awaiting_decision`. §17.1639 dropped a frame only on delete / reset / satisfied; an edit and a step
finishing did neither."""
from __future__ import annotations

import asyncio
import inspect
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.modules import decision_pause as dp
from app.modules import engine_plan as ep
from app.modules import execution_agent as ea

OLD = "Starts from T1. If vendoring: place the pinned charting file under the lab's static asset path and record it."
NEW = "A static file this job carries: `app/static/paper/vendor.json` -- a JSON object naming each third-party file."
FRAME = {"kind": "run", "runner": "pve-runner", "title": "Vendor charting asset",
         "commands": ["pct exec <ENGINE_GUEST_ID> -- sh -c 'mkdir -p <LAB_STATIC_DIR>/vendor'"],
         "verify": ["pct exec <ENGINE_GUEST_ID> -- sh -c 'ls -l <LAB_STATIC_DIR>/vendor/chart.umd.min.js'"],
         "files": [], "options": [{"id": "run"}, {"id": "myself"}, {"id": "skip"}]}


def _asked(**kw):
    return {"node_key": "T4", "prompt_template": OLD, "depends_on": ["T1"], **kw}


# ── the rule, on the live shape and its misses ──────────────────────────

def test_the_live_shape_is_withdrawn():
    # the live frame predates the tool stamp: its text and the step's status carry it
    assert dp.frame_withdrawn_because(_asked(), {"status": "done", "tool": "CodeGen", "prompt_template": NEW,
                                                 "depends_on": ["T1"]}) == "step_done"
    assert dp.frame_withdrawn_because(_asked(), {"status": "pending", "tool": "CodeGen", "prompt_template": NEW,
                                                 "depends_on": ["T1"]}) == "text_changed"
    assert dp.frame_withdrawn_because(_asked(tool="Shell"), {"status": "pending", "tool": "CodeGen",
                                                             "prompt_template": OLD, "depends_on": ["T1"]}) == "tool_changed"
    assert dp.frame_withdrawn_because(_asked(), {"status": "pending", "prompt_template": OLD,
                                                 "depends_on": ["T1", "T3"]}) == "deps_changed"
    assert dp.frame_withdrawn_because(_asked(), {"status": "skipped"}) == "step_skipped"
    assert dp.frame_withdrawn_because(_asked(), None) == "deleted"


def test_a_frame_still_asking_about_its_step_stays():
    same = {"status": "pending", "tool": "shell", "prompt_template": OLD, "depends_on": ["T1"]}
    assert dp.frame_withdrawn_because(_asked(tool="Shell"), same) == ""          # tool compared without case
    assert dp.frame_withdrawn_because(_asked(depends_on=["T3", "T1"]), {**same, "depends_on": ["T1", "T3"]}) == ""
    assert dp.frame_withdrawn_because(_asked(), {**same, "status": "failed"}) == ""
    # an approved run holds its step at `running` with the frame up until the answer is recorded
    assert dp.frame_withdrawn_because(_asked(), {**same, "status": "running", "prompt_template": NEW}) == ""
    # an old frame without the text keys is judged only by what it carries
    assert dp.frame_withdrawn_because({"node_key": "T4"}, {**same, "prompt_template": NEW}) == ""
    assert dp.frame_withdrawn_because({}, None) == ""


def test_a_late_answer_names_the_withdrawal():
    md = {dp.WITHDRAWN_KEY: [{"node_key": "T4", "frame_id": "aaaa", "why": "edited"},
                             {"node_key": "T9", "frame_id": "", "why": "step_done"}]}
    assert dp.withdrawn_record(md, "T4", "aaaa")["why"] == "edited"
    assert dp.withdrawn_record(md, "T9")["why"] == "step_done"
    assert dp.withdrawn_record(md, "T4", "bbbb") is None and dp.withdrawn_record(md, "T5") is None
    assert dp.withdrawn_record(json.dumps(md), "T9") is not None


# ── every writer of a step is covered ───────────────────────────────────

def test_the_run_reads_the_frame_whenever_it_settles_a_step():
    src = inspect.getsource(ea.execute_next_node)
    at = src.index("if not node_written:")
    assert src.index("await _withdraw_stale_frame(db, job_id)", at) < src.index("f\"Node '{title}' -> {final_status}\"")


def test_a_plan_re_apply_that_settles_a_step_drops_its_frame():
    src = inspect.getsource(ep.apply_to_job)
    at = src.index("UPDATE dag_nodes SET status = 'skipped'")
    assert '_drop_parked_frame(db, job_id, [t["id"]], "skipped")' in src[at:at + 1200]
    # the steps it rewrites and resets go through the editor, which drops it itself
    import app.modules.node_editor as ne
    assert '_drop_parked_frame(db, job_id, [node_key], "edited")' in inspect.getsource(ne.edit_node)
    assert '_drop_parked_frame(db, job_id, keys, "reset")' in inspect.getsource(ne._reset_keys)


# ── the live shape, in the test DB ──────────────────────────────────────

async def _job(db, jid: str, t4_status: str = "pending", t3_status: str = "done"):
    text = ea.text
    await db.execute(text("INSERT INTO jobs (id, title, status) VALUES (:id, 'dashboard', 'running')"), {"id": jid})
    for k, st, tool, pt, deps, order in (("T1", "done", "LLM", "Choose delivery", [], 1),
                                        ("T3", t3_status, "LLM", "Pin metric definitions", [], 2),
                                        ("T4", t4_status, "Shell", OLD, ["T1"], 3),
                                        ("T8", "pending", "CodeGen", "Add dashboard route", ["T4"], 4)):
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, status, tool, prompt_template, depends_on, execution_order) "
            "VALUES (:j, :k, :k, :s, :t, :p, :d, :o)"), {"j": jid, "k": k, "s": st, "t": tool, "p": pt, "d": deps, "o": order})
    await db.commit()


async def _park(db, jid: str, key: str = "T4") -> dict:
    node = (await db.execute(ea.text("SELECT node_key, title, tool, prompt_template, depends_on FROM dag_nodes "
                                     "WHERE job_id = :j AND node_key = :k"), {"j": jid, "k": key})).mappings().first()
    asked = await dp.park_awaiting_decision(db, jid, dict(node), dict(FRAME))
    assert asked and asked["frame_id"]
    return asked


async def _state(db, jid: str) -> tuple[str, dict]:
    row = (await db.execute(ea.text("SELECT status, metadata FROM jobs WHERE id = :j"), {"j": jid})).first()
    md = row[1] if isinstance(row[1], dict) else json.loads(row[1] or "{}")
    return row[0], md


async def _drop(jid: str):
    from app.database import async_session
    async with async_session() as db:
        for t in ("dag_node_edits", "dag_nodes"):
            await db.execute(ea.text(f"DELETE FROM {t} WHERE job_id = :j"), {"j": jid})
        await db.execute(ea.text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
        await db.commit()


@pytest.mark.integration
@pytest.mark.timeout(900)
async def test_a_parked_step_rewritten_by_the_plan_loses_its_frame_and_a_late_answer_is_refused():
    from app.database import async_session
    from app.modules import node_editor
    import app.routers.jobs as jr
    from app.schemas import DecideInput
    jid = str(uuid.uuid4())
    try:
        async with async_session() as db:
            await _job(db, jid)
            asked = await _park(db, jid)
            assert asked["tool"] == "Shell"
            assert (await _state(db, jid))[0] == "awaiting_decision"
            # what apply_to_job does to a pending step: its new tool and text, through edit_node
            out = await node_editor.edit_node(jid, "T4", {"tool": "CodeGen", "prompt_template": NEW}, db=db)
            assert out.get("status") == "ok", out
            status, md = await _state(db, jid)
            assert "awaiting_decision" not in md, md
            assert status == "blocked"                       # nothing runs; Run picks the plan up (§17.1211)
            rec = md[dp.WITHDRAWN_KEY][-1]
            assert (rec["node_key"], rec["frame_id"], rec["why"]) == ("T4", asked["frame_id"], "edited")
            # the operator approves the Shell frame still on their screen
            with patch.object(jr, "assert_visible", AsyncMock()), \
                 patch.object(jr.decision_pause, "resolve_decision", AsyncMock()) as resolved:
                with pytest.raises(HTTPException) as e:
                    await jr.decide_endpoint(jid, DecideInput(node_key="T4", choice="run", frame_id=asked["frame_id"]),
                                             db=db, principal=AsyncMock())
            assert e.value.status_code == 409 and "withdrawn" in e.value.detail["error"]
            assert e.value.detail["withdrawn"]["why"] == "edited"
            resolved.assert_not_awaited()
    finally:
        await _drop(jid)


@pytest.mark.integration
@pytest.mark.timeout(900)
async def test_a_parked_step_that_finished_loses_its_frame_whoever_moved_it():
    from app.database import async_session
    jid = str(uuid.uuid4())
    try:
        async with async_session() as db:
            await _job(db, jid)
            asked = await _park(db, jid)
            # live: Run restarted the job (awaiting_decision -> running) and the step was rewritten and finished by
            # writers that never look at the frame
            await db.execute(ea.text("UPDATE jobs SET status = 'running' WHERE id = :j"), {"j": jid})
            await db.execute(ea.text("UPDATE dag_nodes SET tool = 'CodeGen', prompt_template = :p, status = 'done' "
                                     "WHERE job_id = :j AND node_key = 'T4'"), {"j": jid, "p": NEW})
            await db.commit()
            await ea._withdraw_stale_frame(db, jid)
            status, md = await _state(db, jid)
            assert "awaiting_decision" not in md and status == "running"       # a running job keeps its status
            assert md[dp.WITHDRAWN_KEY][-1]["why"] == "step_done"
            assert md[dp.WITHDRAWN_KEY][-1]["frame_id"] == asked["frame_id"]
            await ea._withdraw_stale_frame(db, jid)                            # nothing left: written once
            assert len((await _state(db, jid))[1][dp.WITHDRAWN_KEY]) == 1
    finally:
        await _drop(jid)


@pytest.mark.integration
@pytest.mark.timeout(900)
async def test_another_steps_frame_and_a_run_in_flight_are_left_alone():
    from app.database import async_session
    from app.modules import node_editor
    jid = str(uuid.uuid4())
    try:
        async with async_session() as db:
            await _job(db, jid, t3_status="pending")
            await _park(db, jid, "T3")
            # T4 is rewritten and finishes while T3's question is up
            await node_editor.edit_node(jid, "T4", {"tool": "CodeGen", "prompt_template": NEW}, db=db)
            await db.execute(ea.text("UPDATE dag_nodes SET status = 'done' WHERE job_id = :j AND node_key = 'T4'"),
                             {"j": jid})
            await db.commit()
            await ea._withdraw_stale_frame(db, jid)
            status, md = await _state(db, jid)
            assert status == "awaiting_decision" and md["awaiting_decision"]["node_key"] == "T3"
            assert not md.get(dp.WITHDRAWN_KEY)
            # T3 approved and running its commands: the frame stays up until the answer is recorded
            await db.execute(ea.text("UPDATE dag_nodes SET status = 'running' WHERE job_id = :j AND node_key = 'T3'"),
                             {"j": jid})
            await db.commit()
            await ea._withdraw_stale_frame(db, jid)
            status, md = await _state(db, jid)
            assert status == "awaiting_decision" and md["awaiting_decision"]["node_key"] == "T3"
    finally:
        await _drop(jid)


def test_a_current_answer_is_not_mistaken_for_a_withdrawn_one():
    import app.routers.jobs as jr
    from app.schemas import DecideInput
    md = {"awaiting_decision": {"node_key": "T4", "frame_id": "bbbb"},
          dp.WITHDRAWN_KEY: [{"node_key": "T4", "frame_id": "aaaa", "why": "edited"}]}

    def call(frame_id):
        db = AsyncMock()
        db.execute.return_value.first = lambda: (md,)
        resolved = AsyncMock(return_value={"outcome": "resolved", "node_key": "T4", "record": {}})
        with patch.object(jr, "assert_visible", AsyncMock()), \
             patch.object(jr.decision_pause, "parked_frame_id", AsyncMock(return_value="bbbb")), \
             patch.object(jr.decision_pause, "resolve_decision", resolved):
            try:
                asyncio.run(jr.decide_endpoint(str(uuid.uuid4()), DecideInput(node_key="T4", choice="run",
                                                                              frame_id=frame_id),
                                               db=db, principal=AsyncMock()))
            except HTTPException as e:
                return e, resolved
        return None, resolved

    err, resolved = call("bbbb")                     # the frame parked now
    assert resolved.await_count == 1, err
    err, resolved = call("aaaa")                     # the one withdrawn before it was re-drafted
    assert err is not None and err.status_code == 409 and err.detail["withdrawn"]["why"] == "edited"
    assert resolved.await_count == 0
