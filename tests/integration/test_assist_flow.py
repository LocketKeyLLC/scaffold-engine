"""Integration tests for Assistant Mode against the real Postgres.

Marker: validate (requires the scaffold-orchestrator stack).
Run: docker exec scaffold-orchestrator pytest tests/integration/test_assist_flow.py -m validate -v
"""
from __future__ import annotations

import json
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import async_session
from app.modules import assist_agent


@pytest_asyncio.fixture
async def seeded_job(insert_job):
    """A job in 'planning' with 3 dag_nodes (T1 -> T2 -> T3) and a refined_brief."""
    job_id = await insert_job(
        status="planning",
        title="assist integration test",
        refined_brief={"description": "test goal", "goals": ["g"]},
    )
    nodes = [
        ("T1", "First step", [], 1),
        ("T2", "Second step", ["T1"], 2),
        ("T3", "Third step", ["T2"], 3),
    ]
    async with async_session() as db:
        for nk, title, deps, order in nodes:
            await db.execute(
                text("""
                    INSERT INTO dag_nodes (job_id, node_key, title, depends_on,
                                           execution_order, prompt_template, tool, domain)
                    VALUES (:jid, :nk, :t, :deps, :ord, :pt, 'LLM', 'eng')
                """),
                {"jid": job_id, "nk": nk, "t": title, "deps": deps,
                 "ord": order, "pt": f"prompt for {title}"},
            )
        await db.commit()
    return job_id


@pytest.mark.validate
@pytest.mark.asyncio
async def test_full_walkthrough(seeded_job):
    """Start → next → submit → next → submit → next → submit → completed."""
    job_id = seeded_job
    async with async_session() as db:
        out = await assist_agent.start_assist_session(
            job_id=job_id, replan_policy="disabled", db=db,
        )
        sid = out["session_id"]
    assert out["pending_steps"] == 3

    # Walk all 3 nodes in dependency order.
    for expected_key in ("T1", "T2", "T3"):
        async with async_session() as db:
            step = await assist_agent.get_next_step(session_id=sid, db=db)
        assert step is not None
        assert step["node_key"] == expected_key
        async with async_session() as db:
            await assist_agent.submit_step(
                session_id=sid,
                node_key=expected_key,
                evidence=f"human did {expected_key}",
                evidence_kind="text",
                action="submit",
                db=db,
            )

    # All steps committed -> session + job completed.
    async with async_session() as db:
        sess = await assist_agent.get_session(session_id=sid, db=db)
        job_row = (await db.execute(
            text("SELECT status, compiled_output FROM jobs WHERE id = :id"),
            {"id": job_id},
        )).mappings().first()
    assert sess["status"] == "completed"
    assert job_row["status"] == "completed"

    # Each dag_node has the human evidence mirrored into output_text.
    async with async_session() as db:
        rows = (await db.execute(
            text("SELECT node_key, status, output_text FROM dag_nodes "
                 "WHERE job_id = :id ORDER BY node_key"),
            {"id": job_id},
        )).mappings().all()
    for r in rows:
        assert r["status"] == "done"
        assert r["output_text"].startswith("human did ")


@pytest.mark.validate
@pytest.mark.asyncio
async def test_skip_then_continue(seeded_job):
    """Skipping a node still satisfies downstream deps."""
    job_id = seeded_job
    async with async_session() as db:
        out = await assist_agent.start_assist_session(
            job_id=job_id, replan_policy="disabled", db=db,
        )
        sid = out["session_id"]

    # Skip T1, submit T2 and T3.
    async with async_session() as db:
        step = await assist_agent.get_next_step(session_id=sid, db=db)
        assert step["node_key"] == "T1"
        await assist_agent.submit_step(
            session_id=sid, node_key="T1",
            evidence="", action="skip", db=db,
        )
    for nk in ("T2", "T3"):
        async with async_session() as db:
            step = await assist_agent.get_next_step(session_id=sid, db=db)
            assert step["node_key"] == nk
            await assist_agent.submit_step(
                session_id=sid, node_key=nk,
                evidence=f"out {nk}", action="submit", db=db,
            )

    async with async_session() as db:
        rows = (await db.execute(
            text("SELECT node_key, status FROM dag_nodes WHERE job_id = :id ORDER BY node_key"),
            {"id": job_id},
        )).mappings().all()
    statuses = {r["node_key"]: r["status"] for r in rows}
    assert statuses == {"T1": "skipped", "T2": "done", "T3": "done"}


@pytest.mark.validate
@pytest.mark.asyncio
async def test_idempotent_start(seeded_job):
    """Calling start_assist_session twice on the same job returns the same session."""
    job_id = seeded_job
    async with async_session() as db:
        a = await assist_agent.start_assist_session(job_id=job_id, db=db)
    async with async_session() as db:
        b = await assist_agent.start_assist_session(job_id=job_id, db=db)
    assert a["session_id"] == b["session_id"]


@pytest.mark.validate
@pytest.mark.asyncio
async def test_pause_resume(seeded_job):
    job_id = seeded_job
    async with async_session() as db:
        out = await assist_agent.start_assist_session(job_id=job_id, db=db)
        sid = out["session_id"]
        await assist_agent.pause_session(session_id=sid, db=db)
        sess = await assist_agent.get_session(session_id=sid, db=db)
        assert sess["status"] == "paused"
        await assist_agent.resume_session(session_id=sid, db=db)
        sess = await assist_agent.get_session(session_id=sid, db=db)
        assert sess["status"] == "active"


@pytest.mark.validate
@pytest.mark.asyncio
async def test_friction_log(seeded_job):
    job_id = seeded_job
    async with async_session() as db:
        out = await assist_agent.start_assist_session(job_id=job_id, db=db)
        sid = out["session_id"]
        await assist_agent.record_friction(
            session_id=sid, node_key="T1", note="docs were wrong", db=db,
        )
        await assist_agent.record_friction(
            session_id=sid, node_key="T1", note="took 3 attempts", db=db,
        )
        notes = await assist_agent.list_friction(session_id=sid, db=db)
    assert len(notes) == 1
    assert "docs were wrong" in notes[0]["friction_note"]
    assert "took 3 attempts" in notes[0]["friction_note"]
