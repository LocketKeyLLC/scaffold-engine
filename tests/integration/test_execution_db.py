"""Integration tests: execution_agent helpers against real Postgres.

Specifically:
  * _get_next_node atomic claim under concurrency
  * _compile_output strategy selection from real dag_nodes rows
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.modules.execution_agent import _get_next_node
from app.modules.execution_compile import _compile_output


pytestmark = pytest.mark.asyncio


async def test_get_next_node_returns_first_dep_satisfied(db_session, insert_job):
    job_id = await insert_job()
    # Two pending nodes: T1 with no deps, T2 depending on T1.
    await db_session.execute(text("""
        INSERT INTO dag_nodes (job_id, node_key, title, node_type, status,
                               depends_on, execution_order, tool)
        VALUES (:j, 'T1', 'plan', 'decision', 'pending', '{}', 0, 'LLM'),
               (:j, 'T2', 'do',   'task',     'pending', '{T1}', 1, 'LLM')
    """), {"j": job_id})
    await db_session.commit()

    claim = await _get_next_node(db_session, job_id)
    assert claim is not None
    assert claim["node_key"] == "T1"
    # _get_next_node already flipped T1 to running atomically.
    status = (await db_session.execute(
        text("SELECT status FROM dag_nodes WHERE job_id = :j AND node_key = 'T1'"),
        {"j": job_id},
    )).scalar_one()
    assert status == "running"


async def test_get_next_node_returns_none_when_deps_unsatisfied(db_session, insert_job):
    job_id = await insert_job()
    # Only T2 pending, but its dep T1 isn't done.
    await db_session.execute(text("""
        INSERT INTO dag_nodes (job_id, node_key, title, node_type, status,
                               depends_on, execution_order, tool)
        VALUES (:j, 'T1', 'plan', 'decision', 'failed', '{}', 0, 'LLM'),
               (:j, 'T2', 'do',   'task',     'pending', '{T1}', 1, 'LLM')
    """), {"j": job_id})
    await db_session.commit()

    claim = await _get_next_node(db_session, job_id)
    assert claim is None


# The atomic-claim guarantee of _get_next_node (compound UPDATE … WHERE
# status='pending' RETURNING) is exercised in production under concurrent
# /execute calls. An earlier pytest-level asyncio.gather race proved flaky
# under the shared-fixture setup (the second claimer's UPDATE blocked
# behind the first's row lock within one loop because both claimers
# reused the same ``db_session`` connection). §17.136 re-introduced the
# race in tests/integration/test_execution_concurrency_db.py by giving
# each claimer its own ``async_session()`` (separate asyncpg connection),
# so row-lock arbitration happens at the Postgres layer where it
# belongs. The single-claim / dep-not-satisfied tests above cover the
# per-call behavior; the §17.136 file covers the concurrent-claim
# invariants (no double-claim under any scheduling).


async def _seed_nodes(db_session, job_id, nodes):
    for n in nodes:
        await db_session.execute(text("""
            INSERT INTO dag_nodes (job_id, node_key, title, node_type, status,
                                   depends_on, execution_order, tool,
                                   output_text, is_output_node)
            VALUES (:j, :k, :t, 'task', :s, '{}', :o, :tool, :out, :leaf)
        """), {"j": job_id, "k": n["k"], "t": n["t"], "s": n["s"], "o": n["o"],
                "tool": n["tool"], "out": n["out"], "leaf": n.get("leaf", False)})
    await db_session.commit()


async def test_compile_output_prefers_explicit_leaf(db_session, insert_job):
    """Strategy 0 wins: explicit is_output_node=true outranks last-CodeGen heuristic."""
    job_id = await insert_job()
    await _seed_nodes(db_session, job_id, [
        {"k": "T1", "t": "first",  "s": "done", "o": 0, "tool": "LLM",     "out": "ignored"},
        {"k": "T2", "t": "second", "s": "done", "o": 1, "tool": "CodeGen", "out": "code1"},
        {"k": "T3", "t": "third",  "s": "done", "o": 2, "tool": "LLM",
         "out": "the deliverable", "leaf": True},
    ])
    out, _was_synthesized = await _compile_output(job_id, db_session)
    assert out == "the deliverable"


async def test_compile_output_falls_back_to_last_codegen(db_session, insert_job):
    """No explicit leaf: last CodeGen-tool node is the deliverable."""
    job_id = await insert_job()
    await _seed_nodes(db_session, job_id, [
        {"k": "T1", "t": "first",  "s": "done", "o": 0, "tool": "LLM",     "out": "thinking"},
        {"k": "T2", "t": "second", "s": "done", "o": 1, "tool": "CodeGen", "out": "final code"},
    ])
    out, _was_synthesized = await _compile_output(job_id, db_session)
    assert out == "final code"


async def test_compile_output_concatenates_when_no_codegen(db_session, insert_job):
    """No explicit leaf and last node isn't CodeGen: concatenate done outputs."""
    job_id = await insert_job()
    await _seed_nodes(db_session, job_id, [
        {"k": "T1", "t": "first",  "s": "done", "o": 0, "tool": "LLM", "out": "alpha"},
        {"k": "T2", "t": "second", "s": "done", "o": 1, "tool": "LLM", "out": "beta"},
    ])
    out, _was_synthesized = await _compile_output(job_id, db_session)
    assert "## T1: first" in out
    assert "alpha" in out
    assert "## T2: second" in out
    assert "beta" in out


async def test_compile_output_skips_failed_nodes(db_session, insert_job):
    job_id = await insert_job()
    await _seed_nodes(db_session, job_id, [
        {"k": "T1", "t": "ok",      "s": "done",   "o": 0, "tool": "LLM", "out": "good"},
        {"k": "T2", "t": "bad",     "s": "failed", "o": 1, "tool": "LLM", "out": "broken"},
    ])
    out, _was_synthesized = await _compile_output(job_id, db_session)
    assert "good" in out
    assert "broken" not in out
