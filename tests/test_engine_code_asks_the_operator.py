"""§17.1764 — engine code asks the operator through a job of fixed decisions, and reads the answer when it comes.

Live (job 2772acae T14 "Implement approve-before-run step"): the code parked the decision itself and read
`frame.get("choice")` on the next line -- before anyone could answer -- so every order was rejected. The tests below
run the whole round trip against the real database: open, park (the executor's own `pending_decision` +
`frame_decision`), answer, read, and the handler the asking code registered.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import decision_pause, operator_ask

text = decision_pause.text

CALLS: list[tuple] = []


@operator_ask.answer_handler
async def record_answer(job_id, node_key, answer, payload):
    CALLS.append((node_key, answer, payload))


def unmarked(job_id, node_key, answer, payload):           # not an answer handler
    return None


# ── pure ─────────────────────────────────────────────────────────────────────

def test_a_fixed_decision_is_framed_from_its_own_words_without_the_model():
    node = {"title": "Approve: buy 0.1 SPY at market", "prompt_template": "Risk check passed (5% cap: $1.00).",
            "tool_config": json.dumps({"decision": {"options": ["Approve", "Reject"]}})}
    frame = operator_ask.fixed_frame(node)
    assert frame["question"] == "Approve: buy 0.1 SPY at market?"
    assert [o["label"] for o in frame["options"]] == ["Approve", "Reject"]
    assert frame["suggested"] == "" and frame["fixed"] is True


@pytest.mark.asyncio
async def test_frame_decision_never_calls_the_model_for_a_fixed_decision():
    node = {"title": "Approve?", "tool_config": {"decision": {"options": ["Approve", "Reject"]}}}
    with patch("app.model_router.tool_call", new=AsyncMock(side_effect=AssertionError("model called"))):
        frame = await decision_pause.frame_decision(node, brief="b", upstream="u")
    assert frame["fixed"] is True


def test_a_planner_decision_has_no_fixed_options():
    assert operator_ask.fixed_options({"tool_config": None}) == []
    assert operator_ask.fixed_options({"tool_config": {"timeout": 5}}) == []


@pytest.mark.parametrize("said,picked", [("Approve", "Approve"), ("approve", "Approve"), ("  reject ", "Reject"),
                                         ("yes, approve it", "Approve"), ("maybe", None), ("approve or reject", None)])
def test_an_answer_names_one_option(said, picked):
    assert operator_ask.match_choice(said, ["Approve", "Reject"]) == picked


def test_a_question_is_checked_when_it_is_built():
    with pytest.raises(ValueError):
        operator_ask.decision("bad key!", "Approve?")
    with pytest.raises(ValueError):
        operator_ask.decision("ok", "Approve?", options=["Approve"])
    d = operator_ask.decision("order_1", " Approve: buy SPY? ", "detail", payload={"symbol": "SPY"})
    assert d["question"] == "Approve: buy SPY?" and d["options"] == ["Approve", "Reject"]


def test_only_a_marked_app_function_is_a_handler():
    assert operator_ask._load_handler("tests.test_engine_code_asks_the_operator:record_answer") is None   # not app.*
    assert operator_ask._load_handler("app.modules.operator_ask:fixed_frame") is None                   # not marked
    assert operator_ask._load_handler("os:system") is None


# ── the round trip, on the real database ────────────────────────────────────

async def _cleanup(jid: str) -> None:
    from app.database import async_session
    async with async_session() as db:
        await db.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
        await db.commit()


@pytest.mark.integration
async def test_open_park_answer_read_and_handle():
    from app.database import async_session
    CALLS.clear()
    marked = operator_ask._load_handler
    jid = None
    try:
        with patch.object(operator_ask, "_load_handler",
                          side_effect=lambda d: record_answer if d == "app.lab:record" else marked(d)):
            async with async_session() as db:
                jid = await operator_ask.open_decision_job(
                    db, title="Paper orders 2026-10-24", start=False, on_answer="app.lab:record",
                    decisions=[operator_ask.decision("order_1", "Approve: buy 0.1 SPY at market",
                                                     "5% cap $1.00; risk check passed", payload={"symbol": "SPY"}),
                               operator_ask.decision("order_2", "Approve: buy 0.2 QQQ at market",
                                                     payload={"symbol": "QQQ"})])
            async with async_session() as db:
                assert await operator_ask.answer_of(db, jid, "order_1") is None              # nobody has answered
                node = await decision_pause.pending_decision(db, jid)                         # what the executor does
                assert node["node_key"] == "order_1"
                frame = await decision_pause.frame_decision(node)
                parked = await decision_pause.park_awaiting_decision(db, jid, node, frame)
            assert parked and parked["fixed"] is True

            async with async_session() as db:                                                 # the engine may not
                out = await decision_pause.resolve_decision(db, jid, "order_1", choice=None, delegate=True)
            assert out["outcome"] == "bad_choice" and out["choices"] == ["Approve", "Reject"]
            async with async_session() as db:                                                 # nor a non-option
                out = await decision_pause.resolve_decision(db, jid, "order_1", choice="maybe later")
            assert out["outcome"] == "bad_choice"

            async with async_session() as db:
                out = await decision_pause.resolve_decision(db, jid, "order_1", choice="approve", note="go")
            assert out["outcome"] == "resolved"
            async with async_session() as db:
                ans = await operator_ask.answer_of(db, jid, "order_1")
            assert ans == {"choice": "Approve", "note": "go", "approved": True}
            assert CALLS == [("order_1", ans, {"symbol": "SPY"})]                              # acted on at once

            async with async_session() as db:
                await db.execute(text("UPDATE jobs SET status = 'cancelled' WHERE id = :j"), {"j": jid})
                await db.commit()
                assert await operator_ask.answer_of(db, jid, "order_2") == {"choice": None, "note": "",
                                                                             "approved": False}
    finally:
        if jid:
            await _cleanup(jid)


@pytest.mark.integration
async def test_a_handler_that_is_not_marked_is_refused_when_the_job_opens():
    from app.database import async_session
    async with async_session() as db:
        with pytest.raises(ValueError):
            await operator_ask.open_decision_job(db, title="x", start=False, on_answer="app.modules.operator_ask:fixed_frame",
                                                 decisions=[operator_ask.decision("a", "Approve?")])
        count = (await db.execute(text("SELECT count(*) FROM jobs WHERE title = 'x' AND metadata->>'opened_by' = "
                                       "'operator_ask'"))).scalar()
    assert count == 0


def test_engine_code_that_parks_a_decision_itself_is_refused():
    from app.modules import engine_source as es
    live_t14 = """```python
from app.modules.decision_pause import frame_decision, park_awaiting_decision
```"""
    found = es.unresolved_refs(live_t14)
    assert any("park_awaiting_decision" in f and "operator_ask" in f for f in found)
    ok = """```python
from app.modules.operator_ask import answer_handler, decision, open_decision_job
```"""
    assert es.unresolved_refs(ok) == []
