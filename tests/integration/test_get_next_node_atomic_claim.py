"""§16.5-deferred — atomic-claim coverage extensions for ``_get_next_node``.

Background. The §16.5 audit deferred "live concurrency tests for
``_get_next_node``'s atomic claim under simultaneous /execute calls".
§17.136 landed the baseline (``tests/integration/test_execution_concurrency_db.py``)
covering single-job N-vs-M contention against the real ``dag_nodes``
row lock. This file extends that coverage along three orthogonal axes
§17.136 did not exercise:

  1. **Dependency gating under concurrency.** N claimers race a job that
     has one ``pending`` row whose ``depends_on`` lists an UNFINISHED
     prerequisite. The atomic UPDATE alone cannot be relied on — the
     SELECT-then-filter step in ``_get_next_node`` decides eligibility
     before the row lock fires. Invariant: every claimer returns ``None``
     until the prerequisite is marked ``done``; no row is ever claimed
     with an unsatisfied dep.
  2. **Cross-job isolation.** Two jobs each seed one pending node. N
     claimers per job race concurrently. The ``WHERE job_id = :job_id``
     filter is the production safety net; the test pins it. Invariant:
     each job ends with exactly one ``running`` row, and no claimer
     ever returns a row whose job_id doesn't match its own.
  3. **High fanout.** 10 claimers, 1 pending row. §17.136 stopped at
     N=5. Bumps N to confirm the lock doesn't degrade or surface a
     latent serialization bug under heavier contention.

The fixtures here mirror the §17.136 file: each claimer opens its own
``async_session()`` (one asyncpg connection per claimer) so row-lock
arbitration happens at Postgres, not inside SQLAlchemy. Sharing a
session across ``asyncio.gather`` coroutines deadlocks at the
connection layer, not the row layer — that path is documented as a
flaky harness anti-pattern in §17.136 and is not re-exercised here.
"""
from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.execution_agent import _get_next_node


pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

async def _seed_independent_pending(job_id: str, count: int) -> None:
    """Seed ``count`` independent pending nodes (no deps between them)."""
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


async def _seed_dep_chain(job_id: str) -> None:
    """Seed two pending nodes: T1 (no deps) and T2 (depends on T1).

    The shape exercises the dependency-gating branch of ``_get_next_node``:
    while T1 is ``pending`` and T2's depends_on=['T1'] is unsatisfied, T2
    must NOT be returnable to any claimer.
    """
    async with async_session() as setup_db:
        await setup_db.execute(text(
            "INSERT INTO dag_nodes "
            "(job_id, node_key, title, node_type, status, "
            " depends_on, execution_order, tool) "
            "VALUES "
            "(:j, 'T1', 'first', 'task', 'pending', "
            " '{}', 1, 'LLM'), "
            "(:j, 'T2', 'second', 'task', 'pending', "
            " '{\"T1\"}', 2, 'LLM')"
        ), {"j": job_id})
        await setup_db.commit()


async def _claim_in_own_session(job_id: str) -> dict | None:
    """One claimer = one fresh AsyncSession (its own asyncpg connection).

    Matches the §17.136 pattern. Sharing ``db_session`` across the gather
    set deadlocks at the connection layer (SQLAlchemy serializes
    statements per-connection); fresh sessions push arbitration to
    Postgres where the row lock arbitrates correctly.
    """
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


async def _row_job_ids(claimed_ids: list[str]) -> list[str]:
    """Look up the ``job_id`` for each claimed dag_node row."""
    if not claimed_ids:
        return []
    async with async_session() as db:
        rows = await db.execute(
            text(
                "SELECT job_id FROM dag_nodes WHERE id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": claimed_ids},
        )
        return [str(r[0]) for r in rows]


# ---------------------------------------------------------------------------
# Dependency-gating invariant
# ---------------------------------------------------------------------------

async def test_dependency_unsatisfied_blocks_all_claimers(insert_job):
    """T2 depends on T1; T1 is still pending → 4 claimers all get None.

    ``_get_next_node`` SELECTs all pending rows ordered by
    execution_order, then filters by depends_on satisfaction before
    issuing the atomic UPDATE. With T1 pending and T2's only dep being
    T1, T2 is ineligible. T1 itself IS eligible (no deps), so the
    expected outcome is: exactly one claimer wins T1, the rest return
    None, and T2 is NEVER claimed in this round.
    """
    job_id = await insert_job()
    await _seed_dep_chain(job_id)

    results = await asyncio.gather(*(
        _claim_in_own_session(job_id) for _ in range(4)
    ))

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, (
        f"expected exactly 1 winner under contention, got {len(winners)}: {results}"
    )
    # Hard invariant: the winner must be T1, never T2.
    assert winners[0]["node_key"] == "T1", (
        f"dependency gate failed: claimer won T2 while T1 was still pending — "
        f"winner={winners[0]}"
    )

    statuses_by_key = {}
    async with async_session() as db:
        rows = await db.execute(
            text(
                "SELECT node_key, status FROM dag_nodes WHERE job_id = :j"
            ),
            {"j": job_id},
        )
        for r in rows.mappings():
            statuses_by_key[r["node_key"]] = r["status"]
    assert statuses_by_key == {"T1": "running", "T2": "pending"}, statuses_by_key


async def test_dependency_satisfied_unblocks_next_node(insert_job):
    """Mark T1 done → next round of claimers correctly picks up T2.

    Companion to the gating test above. After T1 transitions to ``done``,
    T2's depends_on is satisfied and a fresh round of N claimers should
    behave exactly like the §17.136 single-row case (1 winner, N-1 None,
    T2 ends ``running``).
    """
    job_id = await insert_job()
    await _seed_dep_chain(job_id)

    # Pre-execute the first round so T1 lands at status='done'. The
    # claim flips to 'running'; the UPDATE below finishes the lifecycle.
    first_round = await _claim_in_own_session(job_id)
    assert first_round is not None
    assert first_round["node_key"] == "T1"
    async with async_session() as db:
        await db.execute(
            text(
                "UPDATE dag_nodes SET status='done', completed_at=NOW() "
                "WHERE id = :id"
            ),
            {"id": str(first_round["id"])},
        )
        await db.commit()

    # Now race the next round. T2 is the only pending row and its dep is
    # satisfied; exactly one claimer must win it.
    results = await asyncio.gather(*(
        _claim_in_own_session(job_id) for _ in range(4)
    ))
    winners = [r for r in results if r is not None]
    assert len(winners) == 1, f"expected exactly 1 winner, got {results}"
    assert winners[0]["node_key"] == "T2", winners[0]

    statuses = await _final_status(job_id)
    assert statuses.get("running", 0) == 1
    assert statuses.get("done", 0) == 1
    assert statuses.get("pending", 0) == 0


# ---------------------------------------------------------------------------
# Cross-job isolation
# ---------------------------------------------------------------------------

async def test_concurrent_claims_across_two_jobs_isolated(insert_job):
    """Two jobs, each with 1 pending node. 4 claimers per job race
    concurrently. Each job must end with exactly one ``running`` row,
    and no claimer ever wins a node from the other job.

    Pins the ``WHERE job_id = :job_id`` filter in ``_get_next_node``.
    A future refactor that dropped or weakened that predicate (e.g.
    by hoisting the SELECT to "all jobs' pending nodes" for batching)
    would let job-A claimers steal job-B rows; this test catches that.
    """
    job_a = await insert_job(title="atomic-claim job A")
    job_b = await insert_job(title="atomic-claim job B")
    await _seed_independent_pending(job_a, count=1)
    await _seed_independent_pending(job_b, count=1)

    # Interleave the two jobs' claimers in one gather to force temporal
    # overlap (gather schedules all coroutines at once; the event loop
    # interleaves their DB calls). Tag each result with the job_id the
    # claimer was acting on so we can verify isolation.
    async def _claim_tagged(job_id):
        result = await _claim_in_own_session(job_id)
        return (job_id, result)

    tagged = await asyncio.gather(*(
        _claim_tagged(job_a if i % 2 == 0 else job_b) for i in range(8)
    ))

    winners_a = [r for jid, r in tagged if jid == job_a and r is not None]
    winners_b = [r for jid, r in tagged if jid == job_b and r is not None]
    assert len(winners_a) == 1, f"job A: expected 1 winner, got {len(winners_a)}"
    assert len(winners_b) == 1, f"job B: expected 1 winner, got {len(winners_b)}"

    # Hard invariant: each winner's row belongs to the job the claimer asked for.
    claimed_a_row_jobs = await _row_job_ids([str(winners_a[0]["id"])])
    claimed_b_row_jobs = await _row_job_ids([str(winners_b[0]["id"])])
    assert claimed_a_row_jobs == [job_a], (
        f"cross-job leak: job-A claimer won a row owned by {claimed_a_row_jobs}"
    )
    assert claimed_b_row_jobs == [job_b], (
        f"cross-job leak: job-B claimer won a row owned by {claimed_b_row_jobs}"
    )

    statuses_a = await _final_status(job_a)
    statuses_b = await _final_status(job_b)
    assert statuses_a == {"running": 1}, statuses_a
    assert statuses_b == {"running": 1}, statuses_b


# ---------------------------------------------------------------------------
# High-fanout stress
# ---------------------------------------------------------------------------

async def test_ten_claimers_one_pending_row(insert_job):
    """N=10 claimers, 1 pending row → exactly 1 winner, 9 None.

    §17.136 capped its fanout at N=5. Bumping to N=10 confirms the
    compound UPDATE's lock doesn't surface a serialization regression
    or starvation pattern under heavier contention. The shape is
    identical to ``test_five_claimers_one_pending_row``; the only
    delta is the gather width.
    """
    job_id = await insert_job()
    await _seed_independent_pending(job_id, count=1)

    results = await asyncio.gather(*(
        _claim_in_own_session(job_id) for _ in range(10)
    ))

    winners = [r for r in results if r is not None]
    losers = [r for r in results if r is None]
    assert len(winners) == 1, (
        f"expected exactly 1 winner under N=10 contention, got {len(winners)}"
    )
    assert len(losers) == 9
    assert winners[0]["node_key"] == "T1"

    # Hard invariant: no double-claim.
    keys = Counter(w["node_key"] for w in winners)
    assert all(c == 1 for c in keys.values()), f"double-claim detected: {dict(keys)}"

    statuses = await _final_status(job_id)
    assert statuses == {"running": 1}
