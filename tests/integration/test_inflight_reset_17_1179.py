"""§17.1179 (audit M8 + the `all_pending_node_keys` race) — a reset must never
clobber a node an executor is writing.

§17.854 (audit A6) hardened ONE statement against this: `execution_retry`'s
root reset re-asserts `status = 'failed'` in its WHERE, with a comment
spelling out the harm ("a running node is flipped back to pending and claimed
by a second executor while the first still writes"). Three sibling statements
that perform the same destructive write never got the predicate:

  * `execution_retry`'s DOWNSTREAM bulk reset (M8),
  * `apply_selective_replan`'s `dag_nodes` reset,
  * and `all_pending_node_keys`, which SELECTS the set that reset consumes
    and included `running` in it.

These run against the real `dag_nodes` row so the predicate is proven by the
database, not by reading the SQL.
"""
from __future__ import annotations

import json

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.assist_replan import all_pending_node_keys

pytestmark = [pytest.mark.asyncio, pytest.mark.integration, pytest.mark.timeout(900)]

#: what an executor has written so far when the racing reset lands
IN_FLIGHT_OUTPUT = "partial output the executor was still streaming"


async def _seed(job_id: str) -> None:
    """R (failed root) → D1 (pending) and D2 (RUNNING, mid-write)."""
    async with async_session() as db:
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, node_type, status, "
            " depends_on, execution_order, tool, output_text) VALUES "
            "(:j,'R','root','task','failed','{}',0,'LLM',NULL), "
            "(:j,'D1','d1','task','pending','{\"R\"}',1,'LLM',NULL), "
            "(:j,'D2','d2','task','running','{\"R\"}',2,'LLM',:out)"
        ), {"j": job_id, "out": IN_FLIGHT_OUTPUT})
        await db.commit()


async def _node(job_id: str, key: str) -> dict:
    async with async_session() as db:
        return dict((await db.execute(text(
            "SELECT status, output_text FROM dag_nodes WHERE job_id=:j AND node_key=:k"
        ), {"j": job_id, "k": key})).mappings().first())


class _ClaimsDuringRetry:
    """A session wrapper that lets a SECOND executor claim D2 in the window
    between the BFS (Stage 4) and the downstream reset (Stage 5).

    Without this the test is vacuous: Stage 4 filters `downstream_to_reset` to
    ('pending','failed'), so a node seeded as 'running' never reaches the
    statement under test and the assertion passes with the fix reverted —
    verified, that is exactly what the first version of this test did. The race
    only exists because the two stages are separate statements, so the test has
    to open that window rather than assume it.
    """

    def __init__(self, inner, job_id: str):
        self._inner, self._job_id, self.claimed = inner, job_id, False

    async def execute(self, stmt, params=None, *a, **kw):
        result = await self._inner.execute(stmt, params, *a, **kw)
        # the root reset is Stage 5a, immediately before the downstream reset
        if not self.claimed and "retry_count  = retry_count + 1" in str(stmt):
            self.claimed = True
            async with async_session() as other:      # a different connection
                await other.execute(text(
                    "UPDATE dag_nodes SET status='running', output_text=:o "
                    " WHERE job_id=:j AND node_key='D2'"
                ), {"j": self._job_id, "o": IN_FLIGHT_OUTPUT})
                await other.commit()
        return result

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def test_the_retry_downstream_reset_leaves_a_node_claimed_mid_retry(insert_job):
    """M8. D2 is 'pending' when the BFS collects it and 'running' by the time
    the reset fires. Without the re-assert its partial output is destroyed and
    it is handed to a second executor while the first still holds it."""
    job_id = await insert_job(status="executing")
    async with async_session() as db:
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, node_type, status, "
            " depends_on, execution_order, tool, output_text) VALUES "
            "(:j,'R','root','task','failed','{}',0,'LLM',NULL), "
            "(:j,'D1','d1','task','pending','{\"R\"}',1,'LLM',NULL), "
            "(:j,'D2','d2','task','pending','{\"R\"}',2,'LLM',NULL)"
        ), {"j": job_id})
        await db.commit()

    from app.modules.execution_retry import retry_failed_node
    async with async_session() as db:
        racing = _ClaimsDuringRetry(db, job_id)
        await retry_failed_node(job_id=job_id, node_key="R", db=racing)
        await db.commit()
        assert racing.claimed, "the race window never opened — the test proves nothing"

    d2 = await _node(job_id, "D2")
    assert d2["status"] == "running", "a node claimed mid-retry was reset out from under its executor"
    assert d2["output_text"] == IN_FLIGHT_OUTPUT, "the second executor's partial write was destroyed"

    d1 = await _node(job_id, "D1")
    assert d1["status"] == "pending", "an untouched downstream node still resets"


async def test_all_pending_node_keys_excludes_in_flight_nodes(insert_job):
    """The SELECT half. Everything this returns is handed to
    `apply_selective_replan` as `affected_override` and reset."""
    job_id = await insert_job(status="assisted_running")
    await _seed(job_id)
    async with async_session() as db:
        keys = await all_pending_node_keys(db=db, job_id=job_id)
    assert "D2" not in keys, f"a running node was offered up for reset: {keys}"
    assert "D1" in keys, f"a pending node must still be included: {keys}"


async def test_the_selective_replan_reset_leaves_a_running_node_alone(insert_job):
    """The write half, proven independently of the SELECT: even when a running
    node is passed in explicitly (the status changed after the set was built),
    the statement must not touch it."""
    job_id = await insert_job(status="assisted_running")
    await _seed(job_id)

    async with async_session() as db:
        reset = [r[0] for r in (await db.execute(text("""
            UPDATE dag_nodes
               SET status = 'pending', output_text = NULL, completed_at = NULL, updated_at = NOW()
             WHERE job_id = :jid AND node_key = ANY(:keys)
               AND status NOT IN ('skipped', 'pending', 'running')
            RETURNING node_key
        """), {"jid": job_id, "keys": ["R", "D1", "D2"]})).fetchall()]
        await db.commit()

    assert "D2" not in reset, "the reset claimed an in-flight node"
    d2 = await _node(job_id, "D2")
    assert (d2["status"], d2["output_text"]) == ("running", IN_FLIGHT_OUTPUT)


async def test_every_destructive_reset_reasserts_status_at_write_time():
    """The class, not the instances. Any UPDATE that nulls `output_text` is
    destroying work; each one must re-assert the row's status in its WHERE, or
    it is racing whatever else holds that row. This is the assertion that makes
    the NEXT such statement fail loudly instead of shipping unguarded.
    """
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[2] / "app" / "modules"
    offenders = []
    for path in (root / "execution_retry.py", root / "assist_replan.py"):
        src = path.read_text()
        for m in re.finditer(r"UPDATE dag_nodes\b.*?(?=\"\"\")", src, re.S):
            stmt = m.group(0)
            if "output_text  = NULL" not in stmt and "output_text = NULL" not in stmt:
                continue
            if "status" not in stmt.split("WHERE", 1)[-1]:
                offenders.append(f"{path.name}: {' '.join(stmt.split())[:110]}")
    assert not offenders, (
        "these statements null output_text without re-asserting status, so they "
        "can overwrite a node another writer holds:\n  " + "\n  ".join(offenders)
    )


async def test_a_retry_retracts_every_compile_artifact_not_just_the_text(insert_job):
    """§17.1179 (audit M9). `compiled_output = NULL` alone left
    `compiled_output_synthesized`, `deliverable_kind` and the
    `metadata.grounding` / `metadata.compile_evidence` records in place, so the
    job advertised a kind and a grounding SCORE for a deliverable it had just
    thrown away — and `observability_rollups` AVG/MINs that score into the
    quality metrics, so the stale value kept being counted.
    """
    job_id = await insert_job(status="failed")
    async with async_session() as db:
        await db.execute(text(
            "INSERT INTO dag_nodes (job_id, node_key, title, node_type, status, "
            " depends_on, execution_order, tool) VALUES "
            "(:j,'R','root','task','failed','{}',0,'LLM')"
        ), {"j": job_id})
        # NB: the seed JSON is BOUND, not inlined. A `:` inside a jsonb literal
        # in `text()` is read by SQLAlchemy as a bind parameter — `:0.91` and
        # `:1` here — and the statement fails with "A value is required for
        # bind parameter '0'". Same family as the jsonb_build_object trap.
        await db.execute(text("""
            UPDATE jobs SET compiled_output = 'the old deliverable',
                            compiled_output_synthesized = TRUE,
                            deliverable_kind = 'executed',
                            metadata = COALESCE(metadata,'{}'::jsonb) || CAST(:md AS jsonb)
             WHERE id = :j
        """), {"j": job_id, "md": json.dumps(
            {"grounding": {"score": 0.91}, "compile_evidence": {"x": 1}, "keep": "me"})})
        await db.commit()

    from app.modules.execution_retry import retry_failed_node
    async with async_session() as db:
        await retry_failed_node(job_id=job_id, node_key="R", db=db)
        await db.commit()

    async with async_session() as db:
        row = dict((await db.execute(text(
            "SELECT compiled_output, compiled_output_synthesized, deliverable_kind, metadata "
            "  FROM jobs WHERE id = :j"
        ), {"j": job_id})).mappings().first())

    assert row["compiled_output"] is None
    assert row["compiled_output_synthesized"] is False, "still claims a synthesized deliverable"
    assert row["deliverable_kind"] is None, "still advertises a deliverable kind"
    md = row["metadata"] or {}
    assert "grounding" not in md, "a stale grounding score survives into the quality rollup"
    assert "compile_evidence" not in md, "stale compile evidence survives"
    assert md.get("keep") == "me", "unrelated job metadata must not be collateral"
