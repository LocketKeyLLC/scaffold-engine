"""§17.1747 — a plan no step of which has run can be planned again from the same brief, through the API.

Live (job f9ca3ee1, 2026-10-21): after each planner fix the Kubernetes plan had the same brief, so `generate_dag`
answered "DAG already exists" (§17.181's input hash), and it was re-planned four times by a script that deleted the
nodes and nulled the hash by hand, with no record of the plan it threw away.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from app.modules import node_editor

# the engine module's own `text`: a test file that mocks sqlalchemy in sys.modules (test_domain_filtering,
# test_validate_dag) or one that purges it (test_scheduler) can leave a different copy behind a bare import
text = node_editor.text


async def _job(db, status: str, nodes: list[tuple[str, str]], decisions: dict | None = None) -> str:
    jid = str(uuid.uuid4())
    meta = {"decisions": decisions} if decisions else {}
    await db.execute(text("INSERT INTO jobs (id, title, status, dag_input_hash, metadata) "
                          "VALUES (:id, 'replan test', :st, 'h', CAST(:m AS JSONB))"),
                     {"id": jid, "st": status, "m": json.dumps({**meta, "awaiting_decision": {"node_key": "T1"}})})
    for i, (key, st) in enumerate(nodes):
        await db.execute(text("INSERT INTO dag_nodes (job_id, node_key, title, status, execution_order) "
                              "VALUES (:id, :k, :t, :st, :o)"), {"id": jid, "k": key, "t": f"Step {key}", "st": st, "o": i})
    await db.commit()
    return jid


@pytest.mark.integration
async def test_an_unstarted_plan_is_cleared_and_kept_in_the_edit_log_and_a_started_one_is_refused():
    from app.database import async_session
    made: list[str] = []
    try:
        async with async_session() as db:
            idle = await _job(db, "executing", [("T1", "pending"), ("T2", "pending")])
            ran = await _job(db, "executing", [("T1", "done"), ("T2", "pending")])
            answered = await _job(db, "awaiting_decision", [("T1", "pending")], decisions={"T1": {"choice": "a"}})
            live = await _job(db, "running", [("T1", "pending")])
            made += [idle, ran, answered, live]

        async with async_session() as db:
            out = await node_editor.clear_an_unstarted_plan(db, idle, edited_by="test")
        assert out == {"cleared": 2, "previous_status": "executing"}
        async with async_session() as db:
            st, h, meta = (await db.execute(text("SELECT status, dag_input_hash, metadata FROM jobs WHERE id = :j"),
                                            {"j": idle})).one()
            n = (await db.execute(text("SELECT COUNT(*) FROM dag_nodes WHERE job_id = :j"), {"j": idle})).scalar()
            log = (await db.execute(text("SELECT op, before FROM dag_node_edits WHERE job_id = :j"), {"j": idle})).all()
        assert (st, h, n) == ("planning", None, 0) and "awaiting_decision" not in (meta or {})
        assert len(log) == 1 and log[0][0] == "replan"
        assert [x["node_key"] for x in log[0][1]["nodes"]] == ["T1", "T2"]          # the old plan is kept

        for jid, says in ((ran, {"started": ["T1"], "answered": []}), (answered, {"started": [], "answered": ["T1"]})):
            async with async_session() as db:
                out = await node_editor.clear_an_unstarted_plan(db, jid)
            assert out["http_status"] == 409 and {k: out[k] for k in says} == says, out
        async with async_session() as db:
            out = await node_editor.clear_an_unstarted_plan(db, live)
        assert out["http_status"] == 409 and "running" in out["error"]
        async with async_session() as db:
            left = (await db.execute(text("SELECT COUNT(*) FROM dag_nodes WHERE job_id IN (:a, :b, :c)"),
                                     {"a": ran, "b": answered, "c": live})).scalar()
        assert left == 4                                                             # refused plans are untouched
    finally:
        async with async_session() as db:
            for jid in made:
                await db.execute(text("DELETE FROM dag_node_edits WHERE job_id = :j"), {"j": jid})
                await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()


async def test_the_route_clears_then_plans_and_reports_both():
    """No database: the endpoint calls the clear, then `generate_dag` with the job's overrides, and returns both."""
    from app.routers import jobs as jr
    from app.authz import ADMIN_PRINCIPAL
    jid = str(uuid.uuid4())
    with patch.object(jr, "assert_visible", AsyncMock()), \
         patch.object(node_editor, "clear_an_unstarted_plan", AsyncMock(return_value={"cleared": 3, "previous_status": "failed"})) as clr, \
         patch.object(jr, "resolve_job_overrides", AsyncMock(return_value={"model_general": "m"})), \
         patch("app.modules.dag_generator.generate_dag", AsyncMock(return_value={"job_id": jid, "task_count": 5})) as gen:
        out = await jr.replan_endpoint(jid, db=AsyncMock(), principal=ADMIN_PRINCIPAL)
    assert out == {"job_id": jid, "task_count": 5, "replanned_from": {"cleared": 3, "previous_status": "failed"}}
    assert clr.await_args.kwargs["edited_by"] == ADMIN_PRINCIPAL.identity
    assert gen.await_args.kwargs["model_overrides"] == {"model_general": "m"}


async def test_a_refused_clear_is_a_409_and_nothing_is_planned():
    from fastapi import HTTPException
    from app.routers import jobs as jr
    from app.authz import ADMIN_PRINCIPAL
    with patch.object(jr, "assert_visible", AsyncMock()), \
         patch.object(node_editor, "clear_an_unstarted_plan",
                      AsyncMock(return_value={"error": "started", "started": ["T1"], "answered": [], "http_status": 409})), \
         patch("app.modules.dag_generator.generate_dag", AsyncMock()) as gen:
        with pytest.raises(HTTPException) as e:
            await jr.replan_endpoint(str(uuid.uuid4()), db=AsyncMock(), principal=ADMIN_PRINCIPAL)
    assert e.value.status_code == 409 and e.value.detail["started"] == ["T1"]
    gen.assert_not_awaited()


def test_running_is_never_a_replan_status():
    assert "running" not in node_editor.REPLAN_FROM
