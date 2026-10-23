"""§17.1779 — a retry sees every earlier refusal of its step, not only the last.

Live (job f00ea97f T7/T8 "Implement EDGAR Form 4 / 13F ingestion", six attempts each, 2026-10-23): one attempt fixed the
sample contact and re-invented `write_ingested_item`; the next fixed that and brought `contact@example.com` back; the
third invented `write_audit_trail_entry`. The retry prompt carried `last_verification_reason` only.
"""
from __future__ import annotations

import json
import uuid

import pytest

from app.modules import execution_agent as xa
from app.modules.execution_retry import _format_reviewer_feedback

SAMPLE = "`contact@example.com` is a sample, not a value."
INVENTED = "`paper_audit_trail_writer` (step T4) has no `write_ingested_item`"
LAST = "`paper_audit_trail_writer` (step T4) has no `write_audit_trail_entry`"


def test_the_retry_block_lists_every_earlier_refusal_once():
    block = _format_reviewer_feedback({"retry_count": 2, "last_verification_reason": LAST,
                                       "prior_refusals": [SAMPLE, INVENTED, LAST]})
    assert LAST in block
    assert "every one must stay fixed" in block and SAMPLE in block and INVENTED in block
    assert block.count(LAST) == 1                                   # the latest is not repeated in the history


def test_a_first_attempt_and_a_step_with_one_refusal_are_unchanged():
    assert _format_reviewer_feedback({"retry_count": 0, "last_verification_reason": LAST,
                                      "prior_refusals": [SAMPLE]}) == ""
    one = _format_reviewer_feedback({"retry_count": 1, "last_verification_reason": LAST, "prior_refusals": [LAST]})
    assert "every one must stay fixed" not in one and LAST in one


@pytest.mark.integration
async def test_prior_refusals_are_read_from_the_run_log():
    from app.database import async_session
    text = xa.text
    jid, nid = str(uuid.uuid4()), None
    try:
        async with async_session() as db:
            await db.execute(text("INSERT INTO jobs (id, title, status) VALUES (:id, 'retry history', 'running')"),
                             {"id": jid})
            nid = (await db.execute(text(
                "INSERT INTO dag_nodes (job_id, node_key, title, status, execution_order) "
                "VALUES (:j, 'T7', 'Implement EDGAR Form 4 ingestion', 'pending', 0) RETURNING id"), {"j": jid})).scalar()
            for msg, reason in [("Node 'Implement EDGAR Form 4 ingestion' -> failed", SAMPLE),
                                ("Node 'Implement EDGAR Form 4 ingestion' evidence: unsupported=0", None),
                                ("Node 'Implement EDGAR Form 4 ingestion' -> failed", INVENTED),
                                ("Node 'Implement EDGAR Form 4 ingestion' -> failed", SAMPLE + "  "),   # a repeat
                                ("Node 'Implement EDGAR Form 4 ingestion' -> failed", LAST)]:
                await db.execute(text("INSERT INTO execution_logs (job_id, node_id, log_level, message, details) "
                                      "VALUES (:j, :n, 'warning', :m, CAST(:d AS jsonb))"),
                                 {"j": jid, "n": nid, "m": msg,
                                  "d": json.dumps({"reason": reason} if reason else {"x": 1})})
            await db.commit()
        async with async_session() as db:
            got = await xa.prior_refusals(db, jid, str(nid))
        assert got == [SAMPLE, INVENTED, LAST]
    finally:
        async with async_session() as db:
            await db.execute(text("DELETE FROM execution_logs WHERE job_id = :j"), {"j": jid})
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()


@pytest.mark.asyncio
async def test_the_executor_puts_the_history_in_the_prompt():
    from unittest.mock import AsyncMock, patch
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, _run
    with patch.object(xa, "prior_refusals", new=AsyncMock(return_value=[SAMPLE, LAST])):
        captured, _, _ = await _run(GROUNDED, {"description": "Write a news CLI.", "goals": ["A CLI."]},
                                    node_extra={"retry_count": 2, "last_verification_reason": LAST})
    assert SAMPLE in captured["prompt"] and "every one must stay fixed" in captured["prompt"]
