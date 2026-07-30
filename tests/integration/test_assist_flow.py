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
async def test_submit_advances_pointer_before_next(seeded_job):
    """§17.638 — submit_step advances current_node_key off the committed step,
    WITHOUT waiting for the next /next claim. Pre-§17.638 the pointer lingered
    on the just-committed step, so every conversational turn re-grounded on it
    and re-rendered its finished walkthrough (the "output is echoing" symptom)."""
    job_id = seeded_job
    async with async_session() as db:
        out = await assist_agent.start_assist_session(
            job_id=job_id, replan_policy="disabled", db=db,
        )
        sid = out["session_id"]
    # Claim + commit T1.
    async with async_session() as db:
        step = await assist_agent.get_next_step(session_id=sid, db=db)
        assert step["node_key"] == "T1"
    async with async_session() as db:
        res = await assist_agent.submit_step(
            session_id=sid, node_key="T1", evidence="did T1",
            evidence_kind="text", action="submit", db=db,
        )
    assert res["next_node_key"] == "T2"
    # The pointer has already moved to T2 — no get_next_step call in between.
    async with async_session() as db:
        sess = await assist_agent.get_session(session_id=sid, db=db)
    assert sess["current_node_key"] == "T2"


@pytest.mark.validate
@pytest.mark.asyncio
async def test_pointer_heals_past_terminal_step(seeded_job):
    """§17.639 — the anti-echo guard. handoff (and other paths) can leave
    current_node_key on a FINISHED step without advancing it; the guard used by
    the guidance path must never hand that finished step back — it self-heals
    forward to the next pending step and persists the corrected pointer, so no
    walkthrough ever re-renders a done step (the "output is echoing" class)."""
    job_id = seeded_job
    async with async_session() as db:
        out = await assist_agent.start_assist_session(
            job_id=job_id, replan_policy="disabled", db=db,
        )
        sid = out["session_id"]
    # Simulate a single handoff of T1: mark the step handed_off and leave the
    # pointer parked on it (exactly what handoff_step does — it never advances).
    async with async_session() as db:
        await db.execute(
            text("UPDATE assist_steps SET status='handed_off' "
                 "WHERE session_id=:sid AND node_key='T1'"),
            {"sid": sid},
        )
        await db.execute(
            text("UPDATE dag_nodes SET status='done' "
                 "WHERE job_id=:jid AND node_key='T1'"),
            {"jid": job_id},
        )
        await db.execute(
            text("UPDATE assist_sessions SET current_node_key='T1' WHERE id=:sid"),
            {"sid": sid},
        )
        await db.commit()
    # Auto-resolve (no explicit node_key) must NOT return the handed-off T1.
    async with async_session() as db:
        resolved = await assist_agent._resolve_live_node_key(
            session_id=sid, node_key=None, current_node_key="T1", db=db,
        )
    assert resolved == "T2"
    # An EXPLICIT request for the finished step is still honored (intentional re-view).
    async with async_session() as db:
        explicit = await assist_agent._resolve_live_node_key(
            session_id=sid, node_key="T1", current_node_key="T1", db=db,
        )
    assert explicit == "T1"
    # The corrected pointer was persisted, so later turns ground on live work.
    async with async_session() as db:
        sess = await assist_agent.get_session(session_id=sid, db=db)
    assert sess["current_node_key"] == "T2"


@pytest.mark.validate
@pytest.mark.asyncio
async def test_full_walkthrough_from_awaiting_assist(seeded_job):
    """§17.625 regression — a job PARKED by the §17.624 hands-on gate in
    'awaiting_assist' must walk start→submit-all→completed just like a
    'planning' job. The original bug: start_assist_session omitted
    'awaiting_assist' from its status-transition IN-list, so the job stayed
    parked and _maybe_finalize_session (WHERE status IN assisted_*) never
    flipped it to 'completed' — the walkthrough finished but the job hung."""
    job_id = seeded_job
    # Park it as the hands-on gate would.
    async with async_session() as db:
        await db.execute(
            text("UPDATE jobs SET status = 'awaiting_assist' WHERE id = :id"),
            {"id": job_id},
        )
        await db.commit()
        out = await assist_agent.start_assist_session(
            job_id=job_id, replan_policy="disabled", db=db,
        )
        sid = out["session_id"]
    assert out["reopened"] is False and out["pending_steps"] == 3

    for expected_key in ("T1", "T2", "T3"):
        async with async_session() as db:
            step = await assist_agent.get_next_step(session_id=sid, db=db)
        assert step is not None and step["node_key"] == expected_key
        async with async_session() as db:
            await assist_agent.submit_step(
                session_id=sid, node_key=expected_key,
                evidence=f"did {expected_key}", evidence_kind="text",
                action="submit", db=db,
            )

    async with async_session() as db:
        sess = await assist_agent.get_session(session_id=sid, db=db)
        job_row = (await db.execute(
            text("SELECT status, deliverable_kind FROM jobs WHERE id = :id"),
            {"id": job_id},
        )).mappings().first()
    assert sess["status"] == "completed"
    assert job_row["status"] == "completed"        # the bug: stayed awaiting_assist
    assert job_row["deliverable_kind"] == "assist_completed"


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


async def _add_one_node(job_id: str) -> None:
    """A single dag_node so list_assist_candidates counts the job (node_count>0)."""
    async with async_session() as db:
        await db.execute(
            text("""
                INSERT INTO dag_nodes (job_id, node_key, title, depends_on,
                                       execution_order, prompt_template, tool, domain)
                VALUES (:jid, 'T1', 'only step', '{}', 1, 'p', 'LLM', 'eng')
            """),
            {"jid": job_id},
        )
        await db.commit()


@pytest.mark.validate
@pytest.mark.asyncio
async def test_candidates_in_progress_excludes_terminal(insert_job):
    """§17.681 — the AUTOMATIC continuity pool (in_progress=True) must exclude
    terminal (completed/cancelled) jobs, so a "continue"/topic-match message can
    never silently re-open a finished or reaper-cancelled job. The explicit-redo
    default (in_progress=False) still lists them."""
    live_id = await insert_job(status="assisted_executing", title="live proxmox build")
    cancelled_id = await insert_job(status="cancelled", title="dead palworld build")
    completed_id = await insert_job(status="completed", title="done vlan build")
    for jid in (live_id, cancelled_id, completed_id):
        await _add_one_node(jid)

    async with async_session() as db:
        in_prog = await assist_agent.list_assist_candidates(db=db, in_progress=True)
        full = await assist_agent.list_assist_candidates(db=db, in_progress=False)

    in_prog_ids = {c["job_id"] for c in in_prog}
    full_ids = {c["job_id"] for c in full}
    # In-progress pool: the live job only.
    assert live_id in in_prog_ids
    assert cancelled_id not in in_prog_ids
    assert completed_id not in in_prog_ids
    # Explicit-redo default: all three re-openable.
    assert {live_id, cancelled_id, completed_id} <= full_ids


@pytest.mark.validate
@pytest.mark.asyncio
async def test_reopen_terminal_session_resets_to_active(seeded_job):
    """§17.681 — re-opening a job whose PRIOR assist session finished must force
    the session back to 'active'. Before the fix, ON CONFLICT only bumped
    last_activity_at, so the reused session kept status='completed' and
    get_next_step returned None — the redo yielded no steps."""
    job_id = seeded_job
    # Run a full walkthrough so session + job reach 'completed'.
    async with async_session() as db:
        out = await assist_agent.start_assist_session(
            job_id=job_id, replan_policy="disabled", db=db,
        )
        sid = out["session_id"]
    for nk in ("T1", "T2", "T3"):
        async with async_session() as db:
            step = await assist_agent.get_next_step(session_id=sid, db=db)
            assert step is not None and step["node_key"] == nk
            await assist_agent.submit_step(
                session_id=sid, node_key=nk, evidence=f"did {nk}",
                evidence_kind="text", action="submit", db=db,
            )
    async with async_session() as db:
        sess = await assist_agent.get_session(session_id=sid, db=db)
    assert sess["status"] == "completed"  # precondition for the reopen test

    # Re-open the now-completed job.
    async with async_session() as db:
        reopened = await assist_agent.start_assist_session(
            job_id=job_id, replan_policy="disabled", db=db,
        )
    # Same session row reused, but forced live.
    assert reopened["session_id"] == sid
    assert reopened["reopened"] is True
    assert reopened["status"] == "active"

    async with async_session() as db:
        sess2 = await assist_agent.get_session(session_id=sid, db=db)
    assert sess2["status"] == "active", "reopened session stuck in terminal status"

    # The redo must actually yield a step (the core symptom of the bug).
    async with async_session() as db:
        step = await assist_agent.get_next_step(session_id=sid, db=db)
    assert step is not None and step["node_key"] == "T1", (
        "reopened session yielded no steps — get_next_step bailed on stale status"
    )


@pytest.mark.validate
@pytest.mark.asyncio
async def test_candidates_in_progress_includes_paused(insert_job):
    """§17.682 — a PAUSED job (assisted_paused) must stay discoverable by
    cross-chat continuity, i.e. appear in the in_progress candidate pool."""
    paused_id = await insert_job(status="assisted_paused", title="paused proxmox build")
    await _add_one_node(paused_id)
    async with async_session() as db:
        in_prog = await assist_agent.list_assist_candidates(db=db, in_progress=True)
    assert paused_id in {c["job_id"] for c in in_prog}


@pytest.mark.validate
@pytest.mark.asyncio
async def test_reconnect_paused_job_resumes_without_reset(seeded_job):
    """§17.682 — reconnecting to a paused job via start_assist_session (the
    continuity path, NOT resume_session) reactivates it AND preserves prior
    work: get_next_step returns the NEXT step, not the already-committed one."""
    job_id = seeded_job
    async with async_session() as db:
        out = await assist_agent.start_assist_session(
            job_id=job_id, replan_policy="disabled", db=db,
        )
        sid = out["session_id"]
    # Commit T1, then pause mid-walkthrough.
    async with async_session() as db:
        step = await assist_agent.get_next_step(session_id=sid, db=db)
        assert step["node_key"] == "T1"
        await assist_agent.submit_step(
            session_id=sid, node_key="T1", evidence="did T1",
            evidence_kind="text", action="submit", db=db,
        )
    async with async_session() as db:
        await assist_agent.pause_session(session_id=sid, db=db)
        sess = await assist_agent.get_session(session_id=sid, db=db)
    assert sess["status"] == "paused"

    # Reconnect via the START path (what cross-chat continuity calls).
    async with async_session() as db:
        reconn = await assist_agent.start_assist_session(
            job_id=job_id, replan_policy="disabled", db=db,
        )
    assert reconn["session_id"] == sid
    assert reconn["reopened"] is False      # a resume, not a terminal redo
    assert reconn["status"] == "active"

    async with async_session() as db:
        sess2 = await assist_agent.get_session(session_id=sid, db=db)
    assert sess2["status"] == "active", "paused session was not reactivated"

    # Work is preserved: T1 stays committed, the next step is T2 (NOT a reset T1).
    async with async_session() as db:
        step = await assist_agent.get_next_step(session_id=sid, db=db)
    assert step is not None and step["node_key"] == "T2", (
        "resume reset the walkthrough — should continue at T2, not redo T1"
    )
    async with async_session() as db:
        t1 = (await db.execute(
            text("SELECT status FROM dag_nodes WHERE job_id=:j AND node_key='T1'"),
            {"j": job_id},
        )).scalar()
    assert t1 == "done", "prior committed node T1 was wrongly reset on resume"
