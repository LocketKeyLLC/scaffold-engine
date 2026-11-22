"""§17.1997 — a new round of a CodeGen step starts with the refusals (and the draft) its last round ended on.

Live (dashboard job 69fe61c4, T8 "Add dashboard route", 2026-11-22): §17.1923 gave T8 two more rounds (dag_node_edits
"engine §17.1923" at 15:35:08 and 15:48:01 UTC), each resetting retry_count to 0. The rounds' first attempts
(llm_traces 15:30:48, 15:40:19, 15:51:52 -- 61,773 chars each, no "Reviewer feedback", no draft) were refused for
the same thing every round: `get_spy_benchmark(…)` called without `start_date`, `end_date`, and no table
`paper_audit_trail`. Two causes: the feedback and the draft were gated on `retry_count > 0`, and the round's own
tool_config edit (`converging_rounds`) closed the §17.1815 window the refusals are read over.
"""
from __future__ import annotations

import inspect
import json
import re
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import engine_plan
from app.modules import execution_agent as xa
from app.modules.execution_retry import _format_reviewer_feedback

SIG = ("§17.1749 this step writes code for the engine itself, and it reaches for things the engine does not have:\n"
       "  - `app/modules/paper_wire_engine_entry_module.py:211` calls `get_spy_benchmark(…)` is missing required "
       "argument(s) `start_date`, `end_date`\n  - `FROM paper_audit_trail`: there is no table `paper_audit_trail`")
SANDBOX = "§17.1765 engine-tests sandbox: its tests failed\n1 failed, 5 passed in 2.31s"
DRAFT = '''```python
# app/modules/paper_wire_engine_entry_module.py
async def dashboard(db):
    return await get_spy_benchmark(db=db)
```
'''


def test_a_round_start_block_carries_the_newest_refusal_the_history_and_the_draft():
    block = _format_reviewer_feedback({"retry_count": 0, "round_refusal": SANDBOX,
                                       "prior_refusals": [SIG, SANDBOX], "prior_draft": DRAFT})
    assert block.startswith("## Reviewer feedback (a new round of this step)")
    assert SANDBOX in block and SIG in block and "every one must stay fixed" in block
    assert block.count(SANDBOX) == 1 and DRAFT.strip() in block and "do not start over" in block


def test_a_first_attempt_without_a_window_refusal_sees_nothing():
    # the row's own reason is never trusted at retry_count 0 (a stale one), only the window's
    assert _format_reviewer_feedback({"retry_count": 0, "last_verification_reason": SIG}) == ""
    assert _format_reviewer_feedback({"retry_count": 0, "prior_refusals": [SIG], "prior_draft": DRAFT}) == ""
    # a retry inside a round reads as before
    assert _format_reviewer_feedback({"retry_count": 1, "last_verification_reason": SIG,
                                      "round_refusal": SANDBOX}).startswith("## Reviewer feedback (attempt 2)")


@pytest.mark.asyncio
async def test_the_snapshot_reads_the_window_on_every_attempt():
    with patch.object(xa, "prior_refusals", new=AsyncMock(return_value=[SIG, SANDBOX])), \
         patch.object(xa, "newest_refusal", new=AsyncMock(return_value=SANDBOX)) as nr, \
         patch.object(xa, "prior_imports", new=AsyncMock(return_value=["paper_benchmarks"])), \
         patch.object(xa, "prior_draft", new=AsyncMock(return_value=DRAFT)):
        snap: dict = {"node_key": "T8", "retry_count": 0}
        await xa._carry_refusals(object(), "job", "node", snap)
        await xa._carry_prior_draft(object(), "job", "node", snap, "CodeGen")
        assert snap["prior_refusals"] == [SIG, SANDBOX] and snap["round_refusal"] == SANDBOX
        assert snap["prior_imports"] == ["paper_benchmarks"] and snap["prior_draft"] == DRAFT
        inside: dict = {"node_key": "T8", "retry_count": 2}              # inside a round: the row's reason serves
        await xa._carry_refusals(object(), "job", "node", inside)
        assert "round_refusal" not in inside and nr.await_count == 1
    with patch.object(xa, "prior_refusals", new=AsyncMock(return_value=[])), \
         patch.object(xa, "prior_imports", new=AsyncMock()) as pi, \
         patch.object(xa, "prior_draft", new=AsyncMock(return_value=DRAFT)) as pd:
        fresh: dict = {"node_key": "T8", "retry_count": 0}                # a fresh step: nothing in the window
        await xa._carry_refusals(object(), "job", "node", fresh)
        await xa._carry_prior_draft(object(), "job", "node", fresh, "CodeGen")
        assert fresh == {"node_key": "T8", "retry_count": 0}
        pi.assert_not_awaited(); pd.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_drafter_prompt_of_a_rounds_first_attempt_carries_the_refusals_and_the_draft():
    """The RETURN: the prompt execute_next_node sends at retry_count 0 after a round's reset."""
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, _run
    with patch.object(xa, "prior_refusals", new=AsyncMock(return_value=[SIG, SANDBOX])), \
         patch.object(xa, "newest_refusal", new=AsyncMock(return_value=SANDBOX)), \
         patch.object(xa, "prior_draft", new=AsyncMock(return_value=DRAFT)):
        captured, _, _ = await _run(GROUNDED, {"description": "Write a news CLI.", "goals": ["A CLI."]},
                                    node_extra={"retry_count": 0, "last_verification_reason": None})
    first = captured["calls"][0][-1]["content"]
    assert "a new round of this step" in first and SANDBOX in first and SIG in first
    assert DRAFT.strip() in first and "do not start over" in first


@pytest.mark.asyncio
async def test_a_fresh_steps_first_prompt_is_unchanged():
    from tests.test_codegen_for_the_engine_reads_the_engine import GROUNDED, _run
    with patch.object(xa, "prior_refusals", new=AsyncMock(return_value=[])), \
         patch.object(xa, "prior_draft", new=AsyncMock(return_value=DRAFT)) as pd:
        captured, _, _ = await _run(GROUNDED, {"description": "Write a news CLI.", "goals": ["A CLI."]})
    first = captured["calls"][0][-1]["content"]
    assert "Reviewer feedback" not in first and "PREVIOUS DRAFT" not in first
    pd.assert_not_awaited()


def test_every_key_the_round_machinery_writes_is_round_bookkeeping():
    """A key §17.1923 / §17.1848 adds to tool_config later must be added to the list, or the window closes again."""
    for fn in (engine_plan.extend_converging_step, engine_plan.switch_code_model):
        written = set(re.findall(r'"(\w+)":', re.search(r"\{\*\*cfg, (.*?)\}", inspect.getsource(fn), re.S).group(1)))
        assert written and written <= set(xa.ROUND_BOOKKEEPING_KEYS), (fn.__name__, written)


@pytest.mark.integration
@pytest.mark.timeout(900)
async def test_the_window_survives_the_engines_round_edits_and_ends_at_a_real_one():
    from app.database import async_session
    text = xa.text
    jid = str(uuid.uuid4())
    log = ("INSERT INTO execution_logs (job_id, node_id, log_level, message, details, created_at) VALUES "
           "(:j, :n, 'warning', 'Node x -> failed', CAST(:d AS jsonb), now() + make_interval(secs => :t))")
    edit = ("INSERT INTO dag_node_edits (job_id, node_key, op, before, after, edited_by, created_at) VALUES "
            "(:j, :k, 'edit', CAST(:b AS jsonb), CAST(:a AS jsonb), :by, now() + make_interval(secs => :t))")
    base = {"fixes_module": "paper_wire_engine_entry_module"}
    switched = {**base, "model_switched_from": "deepseek-v4-pro:cloud", "switched_for": ["missing name"]}
    round1 = {**switched, "converging_rounds": 1, "converging_from": ["3 failed / 3 passed", "1 failed / 5 passed"]}
    try:
        async with async_session() as db:
            await db.execute(text("INSERT INTO jobs (id, title, status) VALUES (:id, 'round memory', 'running')"),
                             {"id": jid})
            ids = {}
            for k in ("T8", "T9", "T10"):
                ids[k] = str((await db.execute(text(
                    "INSERT INTO dag_nodes (job_id, node_key, title, status, execution_order) "
                    "VALUES (:j, :k, 'x', 'pending', 0) RETURNING id"), {"j": jid, "k": k})).scalar())
            for k in ids:
                for t, (reason, draft) in ((1, (SIG, "draft one")), (3, (SANDBOX, "draft two"))):
                    await db.execute(text(log), {"j": jid, "n": ids[k], "t": t,
                                                 "d": json.dumps({"reason": reason, "draft": draft})})
            edits = [  # T8: the engine's own rounds -- a model switch, then a converging round
                ("T8", {"assigned_model": "deepseek-v4-pro:cloud", "tool_config": base},
                 {"assigned_model": "deepseek-v4.1-flash:cloud", "tool_config": switched}, "engine §17.1848", 2),
                ("T8", {"tool_config": switched}, {"tool_config": round1}, "engine §17.1923", 4),
                # T9: a real tool_config change (the module it fixes) cuts the window
                ("T9", {"tool_config": base}, {"tool_config": {**base, "fixes_module": "paper_other"}}, "admin", 4),
                # T10: a text edit cuts it, round keys or not
                ("T10", {"tool_config": switched}, {"tool_config": round1, "prompt_template": "Write `new.py`."},
                 "admin", 4)]
            for k, before, after, by, t in edits:
                await db.execute(text(edit), {"j": jid, "k": k, "b": json.dumps(before), "a": json.dumps(after),
                                              "by": by, "t": t})
            await db.commit()
        async with async_session() as db:
            assert await xa.prior_refusals(db, jid, ids["T8"]) == [SIG, SANDBOX]
            assert await xa.newest_refusal(db, jid, ids["T8"]) == SANDBOX
            assert await xa.prior_draft(db, jid, ids["T8"]) == "draft two"
            for k in ("T9", "T10"):
                assert await xa.prior_refusals(db, jid, ids[k]) == [], k
                assert await xa.newest_refusal(db, jid, ids[k]) == "", k
                assert await xa.prior_draft(db, jid, ids[k]) == "", k
    finally:
        async with async_session() as db:
            for tbl in ("execution_logs", "dag_node_edits"):
                await db.execute(text(f"DELETE FROM {tbl} WHERE job_id = :j"), {"j": jid})
            await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
            await db.commit()
