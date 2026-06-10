"""Integration tests: the §17.466 jobs.completed_at trigger against real Postgres.

Migration 047 adds a BEFORE INSERT OR UPDATE trigger (stamp_job_completed_at)
that maintains the invariant: jobs.completed_at IS NOT NULL <=> status is
terminal (completed / failed / cancelled). These tests exercise that invariant
directly against the live schema the migration runner applied.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

pytestmark = pytest.mark.asyncio


async def _completed_at(db_session, job_id):
    return (await db_session.execute(
        text("SELECT completed_at FROM jobs WHERE id = :j"),
        {"j": job_id},
    )).scalar_one()


async def _set_status(db_session, job_id, status):
    await db_session.execute(
        text("UPDATE jobs SET status = :s WHERE id = :j"),
        {"s": status, "j": job_id},
    )
    await db_session.commit()


@pytest.mark.parametrize("terminal", ["completed", "failed", "cancelled"])
async def test_transition_into_terminal_stamps_completed_at(
    db_session, insert_job, terminal,
):
    """A non-terminal job has NULL completed_at; the UPDATE into a terminal
    state stamps it — covering the exact mainline gap (status flips, the column
    used to stay NULL)."""
    job_id = await insert_job(status="executing")
    assert await _completed_at(db_session, job_id) is None

    await _set_status(db_session, job_id, terminal)
    assert await _completed_at(db_session, job_id) is not None


async def test_reopen_clears_completed_at(db_session, insert_job):
    """A terminal job re-opened to a non-terminal state (the retry_failed_node
    path: blocked/failed -> executing) has its stale completion stamp cleared."""
    job_id = await insert_job(status="executing")
    await _set_status(db_session, job_id, "completed")
    assert await _completed_at(db_session, job_id) is not None

    await _set_status(db_session, job_id, "executing")
    assert await _completed_at(db_session, job_id) is None


async def test_completed_at_is_idempotent_across_later_updates(
    db_session, insert_job,
):
    """Once stamped, a later UPDATE that does not change status (e.g. the
    post-completion compiled_output write) must NOT move completed_at."""
    job_id = await insert_job(status="executing")
    await _set_status(db_session, job_id, "completed")
    first = await _completed_at(db_session, job_id)
    assert first is not None

    # Touch another column the way execution_agent writes compiled_output.
    await db_session.execute(
        text("UPDATE jobs SET compiled_output = 'x' WHERE id = :j"),
        {"j": job_id},
    )
    await db_session.commit()
    assert await _completed_at(db_session, job_id) == first


async def test_direct_terminal_insert_is_stamped(db_session, insert_job):
    """BEFORE INSERT branch: a row inserted directly in a terminal state gets a
    completion stamp without any UPDATE."""
    job_id = await insert_job(status="completed")
    assert await _completed_at(db_session, job_id) is not None


async def test_non_terminal_insert_leaves_completed_at_null(
    db_session, insert_job,
):
    job_id = await insert_job(status="planning")
    assert await _completed_at(db_session, job_id) is None


async def test_no_historical_terminal_job_left_unstamped(db_session):
    """The backfill half of the migration: no terminal job in the table is left
    with a NULL completed_at."""
    leftover = (await db_session.execute(text(
        "SELECT count(*) FROM jobs "
        "WHERE completed_at IS NULL "
        "  AND status IN ('completed', 'failed', 'cancelled')"
    ))).scalar_one()
    assert leftover == 0
