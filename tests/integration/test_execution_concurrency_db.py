"""§17.136 — integration test for `_get_next_node` atomic-claim under concurrency.

Closes the audit gap (§17.53): the compound ``UPDATE … WHERE id=:id AND
status='pending' RETURNING`` claim is the lock that prevents two
concurrent ``/execute/all`` runs from double-executing the same DAG
node. Pre-§17.136 the invariant was only exercised by production
traffic and unit tests against mocked sessions — neither covered the
real Postgres row-locking semantics.

The earlier inline note in ``test_execution_db.py`` recorded a flaky
gather attempt that shared the ``db_session`` fixture between
coroutines (same asyncpg connection → second UPDATE deadlocked behind
the first inside SQLAlchemy). §17.136 fixes the harness by giving each
claimer its own ``async_session()`` (separate asyncpg connection) so
row-lock arbitration happens at the Postgres layer where it belongs.

Invariants exercised (must hold under any event-loop scheduling):

  1. Single-row contention: N claimers, 1 pending row → exactly 1 winner.
     Postgres's row lock makes this deterministic.
  2. Multi-row contention: N claimers, M pending rows → 1..min(N, M)
     distinct winners. The function isn't work-conserving — every
     claimer races for the lowest-execution_order candidate, so the
     second row only gets picked if a later claimer's SELECT happens
     after an earlier claimer's COMMIT. That's a real production
     behavior; ``execute_all_nodes`` loops past the None return.
  3. No double-claim under any scheduling: every claimed row appears
     exactly once across the winner set.
  4. The atomic UPDATE flips status + sets started_at in one statement.

Test matrix:
  - 2 claimers, 1 pending row  → exactly 1 winner + 1 None
  - 5 claimers, 1 pending row  → exactly 1 winner + 4 None
  - 2 claimers, 2 pending rows → 1..2 distinct winners
  - 5 claimers, 2 pending rows → 1..2 distinct winners
  - winners_carry_started_at: side-effect coherence
"""
from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.execution_agent import _get_next_node


pytestmark = pytest.mark.asyncio


async def _seed_pending_rows(job_id: str, count: int) -> None:
    """Seed `count` independent pending nodes (no deps between them)."""
    rows_sql = ",".join(
        f"(:j, 'T{i}', 'node {i}', 'task', 'pending', '{{}}', {i}, 'LLM')"
        for i in range(1, count + 1)
    )
    async with async_session() as setup_db:
        await setup_db.execute(text(
            "INSERT INTO dag_nodes "
            "(job_id, node_key, title, node_type, status, "
            " depends_on, execution_order, tool) "
            f"VALUES {rows_sql}"
        ), {"j": job_id})
        await setup_db.commit()


async def _claim_in_own_session(job_id: str) -> dict | None:
    """One claimer = one fresh AsyncSession (its own asyncpg connection)."""
    async with async_session() as db:
        return await _get_next_node(db, job_id)


async def _final_status(job_id: str) -> dict[str, int]:
    """Final dag_nodes status histogram for the job."""
    async with async_session() as db:
        rows = await db.execute(
            text(
                "SELECT status, COUNT(*) AS c FROM dag_nodes "
                "WHERE job_id = :j GROUP BY status"
            ),
            {"j": job_id},
        )
        return {r["status"]: int(r["c"]) for r in rows.mappings()}


async def test_two_claimers_one_pending_row(insert_job):
    """Two concurrent _get_next_node calls compete for one row.
    Exactly one wins; the other returns None. DB ends with status='running'."""
    job_id = await insert_job()
    await _seed_pending_rows(job_id, count=1)

    results = await asyncio.gather(
        _claim_in_own_session(job_id),
        _claim_in_own_session(job_id),
    )

    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1, (
        f"expected exactly 1 winner under contention, got {len(winners)}: {results}"
    )
    assert len(losers) == 1
    assert winners[0]["node_key"] == "T1"

    statuses = await _final_status(job_id)
    assert statuses == {"running": 1}, f"final statuses {statuses}"


async def test_five_claimers_one_pending_row(insert_job):
    """Five concurrent claimers, one pending row → 1 winner, 4 None.
    Higher fanout proves the lock isn't an artifact of N=2."""
    job_id = await insert_job()
    await _seed_pending_rows(job_id, count=1)

    results = await asyncio.gather(*(
        _claim_in_own_session(job_id) for _ in range(5)
    ))

    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1, f"expected exactly 1 winner, got {len(winners)}"
    assert len(losers) == 4
    assert winners[0]["node_key"] == "T1"

    statuses = await _final_status(job_id)
    assert statuses == {"running": 1}


async def test_two_claimers_two_pending_rows_no_double_claim(insert_job):
    """Two claimers + two pending rows. Possible outcomes:
      - 1 winner (both raced for T1, one won, one returned None)
      - 2 distinct winners (lucky scheduling: claimer B's SELECT landed
        after claimer A's commit, saw T1 as 'running', picked T2)
    The invariant is that no row is ever double-claimed.
    """
    job_id = await insert_job()
    await _seed_pending_rows(job_id, count=2)

    results = await asyncio.gather(
        _claim_in_own_session(job_id),
        _claim_in_own_session(job_id),
    )

    winners = [r for r in results if r is not None]
    assert 1 <= len(winners) <= 2, f"winners outside [1,2]: {results}"

    # Hard invariant: no row claimed twice.
    keys = Counter(w["node_key"] for w in winners)
    assert all(c == 1 for c in keys.values()), (
        f"double-claim detected: {dict(keys)}"
    )

    statuses = await _final_status(job_id)
    # Running count = winner count; rest stayed pending.
    assert statuses.get("running", 0) == len(winners)
    assert statuses.get("pending", 0) == 2 - len(winners)


async def test_five_claimers_two_pending_rows_no_double_claim(insert_job):
    """Five claimers, two pending rows. ``_get_next_node`` is not
    work-conserving: every claimer races for the lowest-execution_order
    candidate, so most pile up on T1 and only the ones whose SELECT
    happens after T1's commit see T2. Outcome: 1..2 distinct winners,
    never a double-claim.
    """
    job_id = await insert_job()
    await _seed_pending_rows(job_id, count=2)

    results = await asyncio.gather(*(
        _claim_in_own_session(job_id) for _ in range(5)
    ))

    winners = [r for r in results if r is not None]
    assert 1 <= len(winners) <= 2, (
        f"winners outside [1,2] for 5 claimers / 2 rows: got {len(winners)}"
    )
    keys = Counter(w["node_key"] for w in winners)
    assert all(c == 1 for c in keys.values()), (
        f"double-claim detected: {dict(keys)}"
    )
    # All winners are subset of the seeded rows
    assert set(keys.keys()) <= {"T1", "T2"}

    statuses = await _final_status(job_id)
    assert statuses.get("running", 0) == len(winners)
    assert statuses.get("pending", 0) == 2 - len(winners)


async def test_winners_carry_started_at_timestamp(insert_job):
    """The atomic UPDATE sets started_at=NOW() in the same statement that
    flips status. Verify the side effect lands on the winner's row."""
    job_id = await insert_job()
    await _seed_pending_rows(job_id, count=1)

    results = await asyncio.gather(
        _claim_in_own_session(job_id),
        _claim_in_own_session(job_id),
    )
    winner = next(r for r in results if r is not None)

    async with async_session() as db:
        row = await db.execute(
            text(
                "SELECT status, started_at FROM dag_nodes WHERE id = :id"
            ),
            {"id": str(winner["id"])},
        )
        record = row.mappings().first()
    assert record["status"] == "running"
    assert record["started_at"] is not None
