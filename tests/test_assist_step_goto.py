"""§17.938 — jump to an arbitrary step, and leave a DURABLE trace.

Two operator reports, one root:

1. *"I refreshed the UI Page and it appears to still be on a forward step."*
   The pointer WAS correct (T21). The transcript was not: a reopen rendered
   with `appendBubble` is client-only, so the reload rebuilt the chat from
   `assist_turns` and its tail still showed the walkthrough of the step the
   operator had been wrongly moved forward TO. The transcript is where the
   operator looks to know where they are, so it has to carry the move.

2. *"a simpler means to jump between the different nodes within the chat,
   perhaps a drop down?"* — the only navigation was ✓ Done (forward one),
   ↩ Back a step (back one, terminal only) and ↻ Re-show (stay put). Reaching
   any other step meant walking the plan or editing the database.
"""
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import assist_agent

pytestmark = pytest.mark.asyncio


def _db(*, session=("job-1", "active"), step=("pending", "pending", "Integrate media stack"),
        guidance="## 👉 Do this next\n```bash\ncurl -s localhost\n```"):
    """execute() returns rows in call order: session → target step → (updates)
    → preserved guidance."""
    db = MagicMock()

    def _row(mapping=None, scalar=None):
        r = MagicMock()
        r.mappings.return_value.first.return_value = mapping
        r.mappings.return_value.all.return_value = mapping if isinstance(mapping, list) else []
        r.scalar.return_value = scalar
        return r

    out = [
        _row({"job_id": session[0], "status": session[1]} if session else None),
        _row({"step_status": step[0], "node_status": step[1], "title": step[2]} if step else None),
        _row(),                 # UPDATE assist_steps
        _row(),                 # UPDATE assist_sessions
        _row(scalar=guidance),  # SELECT guidance
    ]
    db.execute = AsyncMock(side_effect=out + [_row()] * 6)
    db.commit = AsyncMock()
    return db


def _sql(db) -> str:
    return " ".join(str(c.args[0]) for c in db.execute.await_args_list)


# ── the happy path ────────────────────────────────────────────────────────


async def test_moves_the_pointer_and_presents_the_step():
    db = _db()
    res = await assist_agent.goto_step(session_id="s1", node_key="T21", db=db)
    assert res["ok"] is True
    assert res["node_key"] == "T21"
    assert res["title"] == "Integrate media stack"
    sql = _sql(db)
    assert "UPDATE assist_steps" in sql and "status = 'presented'" in sql
    assert "UPDATE assist_sessions SET current_node_key" in sql
    db.commit.assert_awaited()


async def test_preserves_an_existing_walkthrough():
    """§17.901's lesson applies to ARRIVING at a step as much as returning to
    one: regenerating hands the operator different instructions for work they
    may be part-way through."""
    db = _db(guidance="the original walkthrough")
    res = await assist_agent.goto_step(session_id="s1", node_key="T21", db=db)
    assert res["guidance"] == "the original walkthrough"
    assert "ensure_guidance" not in _sql(db)


async def test_presented_at_is_not_overwritten_on_revisit():
    """COALESCE keeps the FIRST time the step was presented — that timestamp
    anchors §17.894 staleness and the §17.886 failure streak."""
    db = _db(step=("presented", "pending", "Integrate media stack"))
    await assist_agent.goto_step(session_id="s1", node_key="T21", db=db)
    assert "COALESCE(presented_at, NOW())" in _sql(db)


# ── the refusals ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", ["committed", "skipped", "handed_off"])
async def test_terminal_steps_are_refused(status):
    """Silently un-completing finished work to satisfy a dropdown selection is
    a plan mutation nobody asked for. ↩ Back a step is the deliberate reopen
    and records that the operator chose it."""
    db = _db(step=(status, "done", "Some finished step"))
    res = await assist_agent.goto_step(session_id="s1", node_key="T3", db=db)
    assert res["ok"] is False
    assert res["reason"] == "terminal_step"
    db.commit.assert_not_awaited()


async def test_a_running_node_is_refused():
    """§17.936's guard, same reasoning: the autonomous executor owns a running
    node, and moving the operator onto it races a live writer."""
    db = _db(step=("pending", "running", "Being executed"))
    res = await assist_agent.goto_step(session_id="s1", node_key="T30", db=db)
    assert res["ok"] is False and res["reason"] == "executor_running"
    db.commit.assert_not_awaited()


async def test_unknown_step_is_refused():
    db = _db(step=None)
    res = await assist_agent.goto_step(session_id="s1", node_key="T999", db=db)
    assert res["ok"] is False and res["reason"] == "unknown_step"
    db.commit.assert_not_awaited()


async def test_missing_and_terminal_sessions_are_refused():
    assert (await assist_agent.goto_step(
        session_id="s1", node_key="T1", db=_db(session=None)))["reason"] == "no_session"
    assert (await assist_agent.goto_step(
        session_id="s1", node_key="T1",
        db=_db(session=("job-1", "completed"))))["reason"] == "session_terminal"


# ── the step list ─────────────────────────────────────────────────────────


async def test_list_steps_orders_the_way_the_plan_runs():
    """Plan order, not alphabetical: `T9` must not sort after `T35`, which is
    exactly what a plain string sort does."""
    db = MagicMock()
    rows = MagicMock()
    rows.mappings.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=rows)
    await assist_agent.list_steps(session_id="s1", db=db)
    sql = " ".join(str(db.execute.call_args[0][0]).split())
    assert "ORDER BY n.execution_order NULLS LAST, length(s.node_key), s.node_key" in sql
    # both halves the picker needs, in one call
    assert "n.title" in sql and "s.status" in sql and "n.status" in sql


async def test_list_steps_fails_soft():
    """A picker must never break the view."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("boom"))
    assert await assist_agent.list_steps(session_id="s1", db=db) == []


# ── the durable trace ─────────────────────────────────────────────────────


async def test_goto_and_step_back_both_record_a_durable_turn():
    """The bug behind "I refreshed and it's still on a forward step": both
    endpoints must write to assist_turns, because the transcript is rebuilt
    from that table on every reload and it is where the operator looks to know
    where they are."""
    import inspect

    from app.routers import assist as assist_router

    goto_src = inspect.getsource(assist_router.assist_goto_step)
    back_src = inspect.getsource(assist_router.assist_step_back)
    for src in (goto_src, back_src):
        assert "ingest_turn" in src
        assert 'kind="track"' in src
