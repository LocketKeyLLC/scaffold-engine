"""§17.2065 — a skipped step settles its job; the skip records why and who.

Live (2026-11-28, job 0fb4ea7f): skipping the three failed steps RDV41, LM5 and CL2 set each row to `skipped` and
stopped there. The job stayed `blocked` -- with every step finished -- until an unrelated POST /execute/all pass found
nothing to do and finalized it; and nothing recorded why the steps were skipped or by whom.

Production path on the real schema: `skip_node` (and POST /skip in-process) against the test database. No mocks.
Every gate uses a plan of more than one step.
"""
from __future__ import annotations

import json
import logging

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.modules import execution_agent as ea

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _drop_skip_edits(tracked_jobs):
    """`dag_node_edits` has no foreign key to `jobs`: the edit rows a skip writes are removed with the test's jobs."""
    yield
    if tracked_jobs:
        async with async_session() as s:
            await s.execute(text("DELETE FROM dag_node_edits WHERE job_id = ANY(CAST(:ids AS uuid[]))"),
                            {"ids": list(tracked_jobs)})
            await s.commit()


async def _seed(db, job_id, nodes):
    """nodes: (key, status, depends_on, output, node_type)"""
    for i, (k, st, deps, out, nt) in enumerate(nodes):
        await db.execute(text("""
            INSERT INTO dag_nodes (job_id, node_key, title, node_type, status, depends_on, execution_order, tool,
                                   prompt_template, output_text, last_verification_reason)
            VALUES (:j, :k, :t, :nt, :s, :d, :o, 'LLM', :p, :out, :why)
        """), {"j": job_id, "k": k, "t": f"step {k}", "nt": nt, "s": st, "d": list(deps),
               "o": i, "p": f"do {k}", "out": out,
               "why": "verifier: the unit file was not found" if st == "failed" else None})
    await db.commit()


async def _job(db, job_id):
    row = (await db.execute(text("SELECT status, compiled_output, metadata FROM jobs WHERE id = :j"),
                            {"j": job_id})).mappings().first()
    md = row["metadata"]
    return row["status"], row["compiled_output"], (json.loads(md) if isinstance(md, str) else (md or {}))


async def _node(db, job_id, key):
    return (await db.execute(text(
        "SELECT status, last_verification_reason FROM dag_nodes WHERE job_id = :j AND node_key = :k"),
        {"j": job_id, "k": key})).mappings().first()


async def _set_status(db, job_id, status, metadata=None):
    await db.execute(text("UPDATE jobs SET status = :s, metadata = CAST(:m AS jsonb) WHERE id = :j"),
                     {"j": job_id, "s": status, "m": json.dumps(metadata or {})})
    await db.commit()


async def test_skipping_the_last_open_step_completes_the_job(db_session, insert_job, caplog):
    """The live case: the failed step was the only open one -> `completed`, with the run's own deliverable."""
    job_id = await insert_job(status="blocked")
    await _seed(db_session, job_id, [
        ("T1", "done", [], "alpha findings", "task"),
        ("T2", "done", ["T1"], "beta findings", "task"),
        ("T3", "failed", ["T2"], None, "task"),
    ])
    with caplog.at_level(logging.INFO, logger=ea.logger.name):
        res = await ea.skip_node(job_id, "T3", db_session, reason="the unit is already installed", by="tester")
    assert res["status"] == "skipped", res
    status, compiled, _ = await _job(db_session, job_id)
    assert status == "completed", res
    assert compiled and "alpha findings" in compiled and "beta findings" in compiled   # the deliverable was compiled
    assert "the job is completed" in res["message"]
    assert any("node_skipped" in r.getMessage() and "by=tester" in r.getMessage()
               and "reason=the unit is already installed" in r.getMessage() for r in caplog.records)


async def test_skipping_a_failed_step_with_work_left_readies_the_job(db_session, insert_job):
    """Blocked only by the step just skipped, other steps still pending -> `executing` (what a retry sets)."""
    job_id = await insert_job(status="blocked")
    await _seed(db_session, job_id, [
        ("T1", "done", [], "alpha", "task"),
        ("T2", "failed", ["T1"], None, "task"),
        ("T3", "pending", ["T2"], None, "task"),
        ("T4", "pending", [], None, "task"),
    ])
    res = await ea.skip_node(job_id, "T2", db_session, reason="done by hand", by="tester")
    assert res["status"] == "skipped", res
    assert (await _job(db_session, job_id))[0] == "executing"
    assert (await _node(db_session, job_id, "T3"))["status"] == "pending"       # the rest is left for the run


async def test_a_failed_job_held_by_the_skipped_step_is_readied(db_session, insert_job):
    job_id = await insert_job(status="failed")
    await _seed(db_session, job_id, [
        ("T1", "failed", [], None, "task"),
        ("T2", "pending", ["T1"], None, "task"),
    ])
    await ea.skip_node(job_id, "T1", db_session, reason="not needed", by="tester")
    assert (await _job(db_session, job_id))[0] == "executing"


async def test_another_failed_step_keeps_the_job_blocked(db_session, insert_job):
    """The correct miss: a second failed step still holds the job."""
    job_id = await insert_job(status="blocked")
    await _seed(db_session, job_id, [
        ("T1", "failed", [], None, "task"),
        ("T2", "failed", [], None, "task"),
        ("T3", "pending", ["T1", "T2"], None, "task"),
    ])
    res = await ea.skip_node(job_id, "T1", db_session, reason="covered elsewhere", by="tester")
    assert res["status"] == "skipped"
    assert (await _job(db_session, job_id))[0] == "blocked"


async def test_a_running_job_is_left_to_its_executor(db_session, insert_job):
    """An /execute/all pass owns a `running` job: the skip settles the step, the run settles the job."""
    job_id = await insert_job(status="running")
    await _seed(db_session, job_id, [
        ("T1", "done", [], "alpha", "task"),
        ("T2", "failed", ["T1"], None, "task"),
    ])
    res = await ea.skip_node(job_id, "T2", db_session, reason="x", by="tester")
    assert res["status"] == "skipped"
    assert (await _job(db_session, job_id))[0] == "running"


@pytest.mark.parametrize("terminal", ["completed", "cancelled"])
async def test_a_skip_never_moves_a_finished_or_cancelled_job(db_session, insert_job, terminal):
    job_id = await insert_job(status=terminal)
    await _seed(db_session, job_id, [
        ("T1", "done", [], "alpha", "task"),
        ("T2", "failed", ["T1"], None, "task"),
        ("T3", "pending", ["T2"], None, "task"),
    ])
    res = await ea.skip_node(job_id, "T2", db_session, reason="x", by="tester")
    assert res["status"] == "error", res
    assert terminal in res["message"]
    assert (await _job(db_session, job_id))[0] == terminal
    assert (await _node(db_session, job_id, "T2"))["status"] == "failed"       # the step is untouched


async def test_the_skip_records_why_and_who_and_keeps_the_pre_image(db_session, insert_job):
    job_id = await insert_job(status="blocked")
    await _seed(db_session, job_id, [
        ("T1", "failed", [], None, "task"),
        ("T2", "pending", ["T1"], None, "task"),
    ])
    await ea.skip_node(job_id, "T1", db_session, reason="the share was mounted by hand", by="tester")
    node = await _node(db_session, job_id, "T1")
    assert node["status"] == "skipped"
    assert node["last_verification_reason"] == "skipped by tester: the share was mounted by hand"
    edit = (await db_session.execute(text(
        "SELECT before, after, edited_by FROM dag_node_edits WHERE job_id = :j AND node_key = 'T1' AND op = 'skip'"),
        {"j": job_id})).mappings().all()
    assert len(edit) == 1
    before = edit[0]["before"] if isinstance(edit[0]["before"], dict) else json.loads(edit[0]["before"])
    assert before == {"status": "failed", "last_verification_reason": "verifier: the unit file was not found"}
    assert edit[0]["edited_by"] == "tester"


async def test_skipping_the_parked_step_withdraws_its_question_and_completes(db_session, insert_job):
    """The job is parked on the step being skipped: the frame is withdrawn the way every settled step withdraws it
    (recorded in `decisions_withdrawn`), and with nothing left open the job completes."""
    job_id = await insert_job(status="blocked")
    await _seed(db_session, job_id, [
        ("T1", "done", [], "alpha", "task"),
        ("T2", "pending", ["T1"], None, "decision"),
    ])
    await _set_status(db_session, job_id, "awaiting_decision", {
        "awaiting_decision": {"node_key": "T2", "frame_id": "f-1", "title": "step T2", "question": "Which way?"}})
    res = await ea.skip_node(job_id, "T2", db_session, reason="decided offline", by="tester")
    assert res["status"] == "skipped", res
    status, _, md = await _job(db_session, job_id)
    assert status == "completed"
    assert "awaiting_decision" not in md
    assert [r["node_key"] for r in md.get("decisions_withdrawn") or []] == ["T2"]


async def test_skipping_another_step_leaves_the_park_alone(db_session, insert_job):
    """The correct miss: the frame asks about T3, the skip is of T2 -- the job keeps asking."""
    job_id = await insert_job(status="blocked")
    await _seed(db_session, job_id, [
        ("T1", "done", [], "alpha", "task"),
        ("T2", "failed", ["T1"], None, "task"),
        ("T3", "pending", ["T1"], None, "decision"),
    ])
    frame = {"node_key": "T3", "frame_id": "f-3", "title": "step T3", "question": "Which way?",
             "tool": "LLM", "prompt_template": "do T3", "depends_on": ["T1"]}
    await _set_status(db_session, job_id, "awaiting_decision", {"awaiting_decision": frame})
    await ea.skip_node(job_id, "T2", db_session, reason="x", by="tester")
    status, _, md = await _job(db_session, job_id)
    assert status == "awaiting_decision"
    assert md["awaiting_decision"]["node_key"] == "T3"


def _api_headers() -> dict[str, str]:
    raw = settings.scaffold_api_key.get_secret_value()
    return {"X-API-Key": raw} if raw else {}


async def test_post_skip_carries_the_reason_and_the_callers_identity(engine_client, db_session, insert_job):
    job_id = await insert_job(status="blocked")
    await _seed(db_session, job_id, [
        ("T1", "done", [], "alpha", "task"),
        ("T2", "failed", ["T1"], None, "task"),
    ])
    async with engine_client(timeout=30.0) as client:
        resp = await client.post("/skip", headers=_api_headers(),
                                 json={"job_id": job_id, "node_key": "T2", "reason": "verified by hand"})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "skipped" and "completed" in body["message"], body
    db_session.expunge_all()
    await db_session.rollback()
    assert (await _job(db_session, job_id))[0] == "completed"
    why = (await _node(db_session, job_id, "T2"))["last_verification_reason"]
    assert why.startswith("skipped by ") and why.endswith(": verified by hand"), why
