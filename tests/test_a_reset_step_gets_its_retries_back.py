"""§17.1752 — resetting a step gives it its retries back; the old count is kept in the reset's pre-image.

Live (job 2772acae T3, 2026-10-22): T3 spent 3/3 attempts on a verifier that never returned a verdict (§17.1751), was
reset, and its one new attempt was refused for a fixable reason (§17.1749). `retry_count` was still 3, so the run
stopped there: a reset was worth exactly one attempt.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.modules import node_editor

text = node_editor.text      # the engine module's own `text` (see test_an_unstarted_plan_can_be_replanned)


@pytest.mark.integration
async def test_an_exhausted_step_is_reset_with_a_fresh_budget_and_the_old_count_is_kept():
    from app.database import async_session
    jid = str(uuid.uuid4())
    try:
        async with async_session() as db:
            await db.execute(text("INSERT INTO jobs (id, title, status, metadata) "
                                  "VALUES (:id, 'reset test', 'blocked', CAST(:m AS JSONB))"),
                             {"id": jid, "m": json.dumps({})})
            await db.execute(text(
                "INSERT INTO dag_nodes (job_id, node_key, title, status, execution_order, retry_count, max_retries, "
                "output_text, last_verification_reason) "
                "VALUES (:id, 'T3', 'Implement SEC EDGAR client', 'failed', 0, 3, 3, 'old', 'verifier produced no tool call')"),
                {"id": jid})
            await db.commit()

        async with async_session() as db:
            out = await node_editor.reset_node(jid, "T3", edited_by="test", db=db)
        assert out["status"] == "ok"
        async with async_session() as db:
            st, rc, mr, txt = (await db.execute(text(
                "SELECT status, retry_count, max_retries, output_text FROM dag_nodes WHERE job_id = :j AND node_key = 'T3'"),
                {"j": jid})).one()
            before = [r[0] for r in (await db.execute(text(
                "SELECT before FROM dag_node_edits WHERE job_id = :j AND node_key = 'T3' AND op = 'reset'"),
                {"j": jid})).all()]
        assert (st, rc, mr, txt) == ("pending", 0, 3, None)
        assert any((b or {}).get("retry_count") == 3 for b in before), before     # the spent count is on record
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM dag_node_edits WHERE job_id = :j"), {"j": jid})
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()
