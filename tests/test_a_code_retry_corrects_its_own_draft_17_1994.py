"""§17.1994 — a CodeGen retry is handed the draft its last refusal points at, and told to correct it, not start over.

Live (dashboard job 69fe61c4, T5 "Write metrics computation module", deepseek-v4-pro, 2026-11-22 14:15-14:26 UTC): four
attempts refused, each with NEW findings (6/5/7/6), the module a different shape each time -- span labels "1w" ->
"1 week" -> "1week" -> "1 week", `exchange_date`'s signature changed. The refusals cite lines ("…_module.py:302 ruff
B905", "tests/…:108 hands exchange_date(…) value = a date"), and the redraft prompts (llm_traces 14:18:31, 14:24:10,
14:25:32, ~34k chars) carried "Reviewer feedback" and none of the draft those lines are in: every retry wrote the module
from nothing. The drafts were stored all along (execution_logs details.draft, §17.1974).
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import execution_agent as xa
from app.modules.execution_retry import _format_reviewer_feedback

DRAFT = '''```python
# app/modules/span_metrics.py
SPANS = {"1w": 7, "1m": 30}

def exchange_date(value):
    return value.date()
```
'''
REASON = "`tests/test_span_metrics.py:12` hands `exchange_date(…)` `value` = a date (line 11)"


def test_the_retry_block_carries_the_draft_and_says_correct_it():
    block = _format_reviewer_feedback({"retry_count": 1, "last_verification_reason": REASON, "prior_draft": DRAFT})
    assert REASON in block and DRAFT.strip() in block
    assert "do not start over" in block and "line numbers" in block
    assert block.index(REASON) < block.index("<<<<< PREVIOUS DRAFT") < block.index(DRAFT.strip())
    assert block.rstrip().endswith("---")                         # still one block, before the task


def test_no_draft_no_block_and_a_first_attempt_sees_nothing():
    plain = _format_reviewer_feedback({"retry_count": 1, "last_verification_reason": REASON})
    assert "PREVIOUS DRAFT" not in plain and REASON in plain
    assert _format_reviewer_feedback({"retry_count": 0, "last_verification_reason": REASON, "prior_draft": DRAFT}) == ""


@pytest.mark.asyncio
async def test_only_a_code_steps_retry_is_handed_the_draft():
    with patch.object(xa, "prior_draft", new=AsyncMock(return_value=DRAFT)) as pd:
        for tool, retry, want in (("CodeGen", 1, True), ("codegen", 2, True), ("Shell", 1, False),
                                  ("LLM", 1, False), ("CodeGen", 0, False)):
            snap: dict = {"node_key": "T5", "retry_count": retry}
            await xa._carry_prior_draft(object(), "job", "node", snap, tool)
            assert ("prior_draft" in snap) is want, (tool, retry)
        assert pd.await_count == 2
    with patch.object(xa, "prior_draft", new=AsyncMock(side_effect=RuntimeError("db gone"))):
        snap = {"node_key": "T5", "retry_count": 1}
        await xa._carry_prior_draft(object(), "job", "node", snap, "CodeGen")   # a failed read never fails the node
        assert "prior_draft" not in snap


@pytest.mark.asyncio
async def test_the_drafter_prompt_carries_the_draft():
    """The RETURN: the prompt the code model is called with, through execute_next_node."""
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, _run
    with patch.object(xa, "prior_draft", new=AsyncMock(return_value=DRAFT)), \
         patch.object(xa, "prior_refusals", new=AsyncMock(return_value=[REASON])):
        captured, _, _ = await _run(GROUNDED, {"description": "Write a news CLI.", "goals": ["A CLI."]},
                                    node_extra={"retry_count": 1, "last_verification_reason": REASON})
    first = captured["calls"][0][-1]["content"]
    assert REASON in first and DRAFT.strip() in first and "do not start over" in first


@pytest.mark.asyncio
async def test_a_first_attempt_prompt_has_no_draft():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, _run
    with patch.object(xa, "prior_draft", new=AsyncMock(return_value=DRAFT)) as pd:
        captured, _, _ = await _run(GROUNDED, {"description": "Write a news CLI.", "goals": ["A CLI."]})
    assert "PREVIOUS DRAFT" not in captured["calls"][0][-1]["content"]
    pd.assert_not_awaited()


@pytest.mark.integration
@pytest.mark.timeout(900)
async def test_the_draft_is_the_newest_refused_ones_since_the_step_changed():
    from app.database import async_session
    text = xa.text
    jid = str(uuid.uuid4())
    log = ("INSERT INTO execution_logs (job_id, node_id, log_level, message, details, created_at) VALUES "
           "(:j, :n, 'warning', :m, CAST(:d AS jsonb), now() + make_interval(secs => :t))")
    edit = ("INSERT INTO dag_node_edits (job_id, node_key, op, after, edited_by, created_at) VALUES "
            "(:j, :k, :op, CAST(:a AS jsonb), 'test', now() + make_interval(secs => :t))")
    try:
        async with async_session() as db:
            await db.execute(text("INSERT INTO jobs (id, title, status) VALUES (:id, 'draft carry', 'running')"),
                             {"id": jid})
            ids = []
            for k in ("T5", "T6", "T7"):
                ids.append((await db.execute(text(
                    "INSERT INTO dag_nodes (job_id, node_key, title, status, execution_order) "
                    "VALUES (:j, :k, 'x', 'pending', 0) RETURNING id"), {"j": jid, "k": k})).scalar())
            t5, t6, t7 = ids
            rows = [(t5, 1, "Node x -> failed", {"reason": "a1", "draft": "draft one"}),
                    (t5, 3, "Node x -> failed", {"reason": "a2", "draft": "draft two"}),
                    (t5, 4, "Node x evidence: unsupported=6", {"draft": "not a refusal"}),
                    (t6, 1, "Node x -> failed", {"reason": "old", "draft": "old version's draft"}),
                    (t7, 1, "Node x -> failed", {"reason": "b1", "draft": "draft b1"}),
                    (t7, 3, "Node x -> failed", {"reason": "timed out"})]          # the last refusal left none
            for n, t, m, d in rows:
                await db.execute(text(log), {"j": jid, "n": n, "m": m, "d": json.dumps(d), "t": t})
            for k, op, after, t in (("T5", "reset", {"status": "pending"}, 2),            # a retry keeps it
                                    ("T6", "edit", {"prompt_template": "Write `new.py`."}, 2)):   # a new text cuts it
                await db.execute(text(edit), {"j": jid, "k": k, "op": op, "a": json.dumps(after), "t": t})
            await db.commit()
        async with async_session() as db:
            assert await xa.prior_draft(db, jid, str(t5)) == "draft two"
            assert await xa.prior_draft(db, jid, str(t6)) == ""
            assert await xa.prior_draft(db, jid, str(t7)) == ""      # its lines are not the refusal's lines
    finally:
        async with async_session() as db:
            for tbl in ("execution_logs", "dag_node_edits"):
                await db.execute(text(f"DELETE FROM {tbl} WHERE job_id = :j"), {"j": jid})
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()


@pytest.mark.asyncio
async def test_a_long_draft_keeps_its_head_and_its_tail():
    long = "# app/modules/big.py\n" + "x = 1\n" * 20_000 + "# tests/test_big.py\ndef test_last():\n    pass\n"
    res = AsyncMock()
    res.scalars = lambda: type("S", (), {"first": lambda self: long})()
    db = AsyncMock()
    db.execute = AsyncMock(return_value=res)
    kept = await xa.prior_draft(db, "j", "n")
    assert len(kept) < xa._PRIOR_DRAFT_KEEP + 100
    assert kept.startswith("# app/modules/big.py") and kept.endswith("def test_last():\n    pass")
    assert f"[§17.1994: {len(long.strip()) - xa._PRIOR_DRAFT_KEEP} chars of this draft cut]" in kept
