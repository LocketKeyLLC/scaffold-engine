"""§17.901 — "↩ Back a step": undo the last completed step.

`✓ Done → next step` was a one-way door. An operator mis-clicked it, closing a
step they had not finished, and the only nearby verb — `↻ Re-show step` —
re-presents whatever the pointer moved TO, i.e. the NEXT step. That is the
"redo a step brings it to a weird place" report: there was no way back at all.

Two properties matter and both are tested here:

1. The reopen mirrors §17.286 across `dag_nodes` + `assist_steps` + the session
   pointer, and clears the node's fabricated output.
2. It PRESERVES the walkthrough, and hands it back to the caller. This is not
   an optimization. The reopen sets `dag_nodes.updated_at = NOW()`, which trips
   §17.894's `replanned` staleness probe — so re-running the guide pipeline
   after a step-back would REGENERATE and hand the operator different
   instructions for work they were part-way through, recreating the exact bug.
   Contrast §17.899's denial reopen, which redraws on purpose: there the
   project really has moved on.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import assist_agent

pytestmark = pytest.mark.asyncio


def _db(*, session=("job-1", "active", "T24"),
        target=("T23", "committed"), title="Install PalWorld server",
        node_status="done",
        guidance="## 👉 Do this next\n```bash\nsudo apt update\n```"):
    """execute() returns rows in call order: session → target step → node title
    → preserved guidance, then the UPDATEs."""
    db = MagicMock()
    out = []

    def _row(mapping=None, scalar=None):
        r = MagicMock()
        r.mappings.return_value.first.return_value = mapping
        r.scalar.return_value = scalar
        return r

    out.append(_row({"job_id": session[0], "status": session[1],
                     "current_node_key": session[2]} if session else None))
    out.append(_row({"node_key": target[0], "status": target[1]} if target else None))
    out.append(_row({"title": title, "status": node_status}))
    out.append(_row(scalar=guidance))
    db.execute = AsyncMock(side_effect=out + [_row()] * 8)
    db.commit = AsyncMock()
    return db


def _sql(db) -> str:
    return " ".join(str(c.args[0]) for c in db.execute.await_args_list)


async def test_reopens_the_last_completed_step():
    db = _db()
    res = await assist_agent.step_back(session_id="s1", db=db)
    assert res["node_key"] == "T23"
    assert res["title"] == "Install PalWorld server"
    assert res["was"] == "committed"
    db.commit.assert_awaited()


async def test_walkthrough_is_preserved_and_returned():
    """The caller re-renders THIS text instead of calling the guide pipeline —
    see the module docstring for why regenerating recreates the bug."""
    db = _db(guidance="the exact walkthrough they were working from")
    res = await assist_agent.step_back(session_id="s1", db=db)
    assert res["guidance"] == "the exact walkthrough they were working from"
    sql = _sql(db).replace(" = ", "=")
    # The preserving branch must NOT null the guidance columns…
    assert "guidance=NULL" not in sql
    assert "guidance_status='none'" not in sql
    # …and must re-present rather than drop back to pending.
    assert "status='presented'" in sql


async def test_mirrors_all_three_tables_and_clears_the_node_output():
    db = _db()
    await assist_agent.step_back(session_id="s1", db=db)
    sql = _sql(db).replace(" = ", "=")
    assert "UPDATE dag_nodes" in sql
    assert "UPDATE assist_steps" in sql
    assert "UPDATE assist_sessions" in sql
    # The commit's fabricated output must go — it poisons the completed-work
    # digest every later step reads.
    assert "output_text=NULL" in sql
    assert "committed_at=NULL" in sql


async def test_a_skipped_step_can_also_be_stepped_back():
    """⏩ Skip is exactly as mis-clickable as ✓ Done, and strands the operator
    forward of where they meant to be in the same way."""
    db = _db(target=("T20", "skipped"))
    res = await assist_agent.step_back(session_id="s1", db=db)
    assert res["node_key"] == "T20"
    assert res["was"] == "skipped"


async def test_explicit_node_key_targets_that_step():
    db = _db(target=("T18", "committed"))
    res = await assist_agent.step_back(session_id="s1", node_key="T18", db=db)
    assert res["node_key"] == "T18"


async def test_explicit_node_key_that_is_not_completed_is_refused():
    """Reopening a step that never closed would move the pointer BACKWARD past
    live work — a silent plan mutation, not an undo."""
    db = _db(target=("T25", "pending"))
    assert await assist_agent.step_back(session_id="s1", node_key="T25", db=db) is None
    db.commit.assert_not_awaited()


async def test_nothing_completed_yet_returns_none():
    db = _db(target=None)
    assert await assist_agent.step_back(session_id="s1", db=db) is None
    db.commit.assert_not_awaited()


async def test_terminal_session_is_a_noop():
    db = _db(session=("job-1", "completed", "T24"))
    assert await assist_agent.step_back(session_id="s1", db=db) is None
    db.commit.assert_not_awaited()


async def test_missing_session_is_a_noop():
    db = _db(session=None)
    assert await assist_agent.step_back(session_id="s1", db=db) is None
    db.commit.assert_not_awaited()


async def test_db_error_fails_soft():
    """A failed undo must report cleanly, never trap the operator's session."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("boom"))
    db.commit = AsyncMock()
    assert await assist_agent.step_back(session_id="s1", db=db) is None


async def test_denial_reopen_still_DROPS_guidance():
    """The sibling path must keep its opposite behavior: a §17.899 denial means
    the project moved on, so the stale walkthrough is redrawn (§17.894)."""
    db = MagicMock()
    rows = []

    def _row(mapping=None, scalar=None):
        r = MagicMock()
        r.mappings.return_value.first.return_value = mapping
        r.scalar.return_value = scalar
        return r

    rows.append(_row({"job_id": "job-1", "status": "active"}))          # session
    rows.append(_row({"node_key": "T23", "committed_at": "2026-09-01T23:13:18Z",
                      "age_s": 60.0}))                                  # last commit
    rows.append(_row(scalar=1))                                         # turns since
    rows.append(_row({"title": "Install PalWorld server",
                      "output_text": "It worked"}))                     # node
    db.execute = AsyncMock(side_effect=rows + [_row()] * 8)
    db.commit = AsyncMock()

    out = await assist_agent.reopen_denied_step(
        session_id="s1", message="we have not installed anything else", db=db)
    assert out is not None
    sql = _sql(db).replace(" = ", "=")
    assert "guidance=NULL" in sql
    assert "guidance_status='none'" in sql


# ── §17.936 — 🤝 Engine does it is a one-way door too ─────────────────────


async def test_a_handed_off_step_can_be_stepped_back():
    """The gap that made this real: a handed-off step could not be reopened at
    all, so `↩ Back a step` 409'd and the only repair was hand-written SQL
    against all three mirrored tables.

    Found on the operator's live session — a stray write handed off T26
    ("Install AI VM OS") and advanced them to T27, i.e. off the install they
    had been working for days. `🤝 Engine does it` moves the pointer forward
    off a step the operator may not have finished, exactly like ✓ Done and
    ⏩ Skip, so it belongs in the same undo."""
    db = _db(target=("T26", "handed_off"), node_status="pending")
    res = await assist_agent.step_back(session_id="s1", db=db)
    assert res["node_key"] == "T26"
    assert res["was"] == "handed_off"
    db.commit.assert_awaited()


async def test_handed_off_step_is_refused_while_the_executor_runs_it():
    """A handed-off step may be IN FLIGHT: the autonomous executor claims the
    node as `running`. Reopening underneath it would race a live writer and
    leave the two tables disagreeing about who owns the step. Committed and
    skipped steps can never be in this state, so the guard is specific to
    handed_off."""
    db = _db(target=("T26", "handed_off"), node_status="running")
    assert await assist_agent.step_back(session_id="s1", node_key="T26", db=db) is None
    db.commit.assert_not_awaited()


async def test_committed_step_is_not_blocked_by_a_running_node():
    """The race guard must not over-reach: only handed_off is gated on it."""
    db = _db(target=("T23", "committed"), node_status="running")
    res = await assist_agent.step_back(session_id="s1", db=db)
    assert res["node_key"] == "T23"


async def test_handed_off_is_reachable_without_an_explicit_node_key():
    """The implicit 'most recent terminal step' query must include the new
    status too, or ↩ Back a step still does nothing on the bare button."""
    db = _db(target=("T26", "handed_off"), node_status="pending")
    await assist_agent.step_back(session_id="s1", db=db)
    sql = _sql(db)
    assert "status = ANY(:states)" in sql
    states = next(c.args[1]["states"] for c in db.execute.await_args_list
                  if len(c.args) > 1 and isinstance(c.args[1], dict)
                  and "states" in c.args[1])
    assert set(states) == {"committed", "skipped", "handed_off"}

