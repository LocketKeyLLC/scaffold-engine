"""§17.568 — atomic-claim coverage for ``_claim_ready_nodes`` (parallel frontier).

Companion to ``test_get_next_node_atomic_claim.py``. ``_claim_ready_nodes`` is
the multi-node atomic claim that backs parallel-frontier execution: it folds
the deps-satisfied predicate into the claim's WHERE (NOT EXISTS over unfinished
deps) + ``FOR UPDATE SKIP LOCKED LIMIT`` so concurrent claimers get DISJOINT,
dependency-correct frontiers. These tests exercise it against the real
``dag_nodes`` row lock (each claimer its own ``async_session`` — sharing one
across ``asyncio.gather`` deadlocks at the connection layer, per §17.136).
"""
from __future__ import annotations

import asyncio
from collections import Counter

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.execution_agent import _claim_ready_nodes


pytestmark = pytest.mark.asyncio


async def _seed_diamond(job_id: str) -> None:
    """T1 → {T2, T3} → T4."""
    async with async_session() as db:
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, node_type, status, "
            " depends_on, execution_order, tool) VALUES "
            "(:j,'T1','t1','task','pending','{}',0,'LLM'), "
            "(:j,'T2','t2','task','pending','{\"T1\"}',1,'LLM'), "
            "(:j,'T3','t3','task','pending','{\"T1\"}',2,'LLM'), "
            "(:j,'T4','t4','task','pending','{\"T2\",\"T3\"}',3,'LLM')"
        ), {"j": job_id})
        await db.commit()


async def _seed_independent(job_id: str, count: int) -> None:
    rows = ",".join(
        f"(:j,'T{i}','n{i}','task','pending','{{}}',{i},'LLM')"
        for i in range(1, count + 1)
    )
    async with async_session() as db:
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, node_type, status, "
            f"depends_on, execution_order, tool) VALUES {rows}"
        ), {"j": job_id})
        await db.commit()


async def _mark_done(job_id: str, *keys: str) -> None:
    async with async_session() as db:
        await db.execute(text(
            "UPDATE dag_nodes SET status='done', completed_at=NOW() "
            "WHERE job_id=:j AND node_key = ANY(:k)"
        ), {"j": job_id, "k": list(keys)})
        await db.commit()


async def test_claim_ready_nodes_respects_deps_wave_by_wave(insert_job):
    """The diamond reveals one wave at a time as deps complete."""
    job_id = await insert_job()
    await _seed_diamond(job_id)

    async with async_session() as db:
        w1 = await _claim_ready_nodes(db, job_id, 10)
    assert sorted(n["node_key"] for n in w1) == ["T1"], "only T1 has satisfied deps"

    await _mark_done(job_id, "T1")
    async with async_session() as db:
        w2 = await _claim_ready_nodes(db, job_id, 10)
    assert sorted(n["node_key"] for n in w2) == ["T2", "T3"], "T1 done unblocks T2,T3"

    await _mark_done(job_id, "T2", "T3")
    async with async_session() as db:
        w3 = await _claim_ready_nodes(db, job_id, 10)
    assert sorted(n["node_key"] for n in w3) == ["T4"]

    await _mark_done(job_id, "T4")
    async with async_session() as db:
        w4 = await _claim_ready_nodes(db, job_id, 10)
    assert w4 == []


async def test_claim_ready_nodes_never_claims_unsatisfied_dep(insert_job):
    """T4 (deps T2,T3) is never in a wave until BOTH deps are done — even
    after only one dep completes."""
    job_id = await insert_job()
    await _seed_diamond(job_id)
    await _mark_done(job_id, "T1")
    await _mark_done(job_id, "T2")  # T3 still pending → T4 must stay blocked
    async with async_session() as db:
        wave = await _claim_ready_nodes(db, job_id, 10)
    keys = {n["node_key"] for n in wave}
    assert "T4" not in keys, f"T4 claimed with unsatisfied dep T3: {keys}"
    assert keys == {"T3"}, keys  # only the now-ready T3


async def test_concurrent_claimers_get_disjoint_frontier(insert_job):
    """6 independent pending nodes, 6 concurrent claimers (each limit=6, own
    session) → every node claimed exactly once across claimers, no overlap,
    no double-claim (FOR UPDATE SKIP LOCKED)."""
    job_id = await insert_job()
    await _seed_independent(job_id, count=6)

    async def _claim():
        async with async_session() as db:
            return await _claim_ready_nodes(db, job_id, 6)

    batches = await asyncio.gather(*(_claim() for _ in range(6)))
    all_keys = [n["node_key"] for b in batches for n in b]

    # Exactly the 6 nodes, each claimed once — disjoint across claimers.
    assert sorted(all_keys) == ["T1", "T2", "T3", "T4", "T5", "T6"]
    dupes = {k: c for k, c in Counter(all_keys).items() if c > 1}
    assert not dupes, f"double-claim under concurrency: {dupes}"

    async with async_session() as db:
        rows = await db.execute(text(
            "SELECT COUNT(*) FROM dag_nodes WHERE job_id=:j AND status='running'"
        ), {"j": job_id})
        assert rows.scalar() == 6
