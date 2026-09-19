"""§17.1112 (Phase 1 ledger D-1 / D-2) — the cursor invariant on goto, reopen
and retire.

D-1: a forward goto (and a reopen) presented the target and moved the cursor
but never un-claimed the step the operator left; `_load_presented_step` picks
the EARLIEST presented step, so the §17.1103 self-heal snapped the cursor back
one turn later. Now: one presented step per session.

D-2: the tracker's retire always moved the pointer, even when it retired a
step that was NOT the cursor, and committed from `pending` (which the FSM
forbids). Now: present-then-commit through the oracle; the pointer moves only
when the retired step is the cursor (or the cursor is empty).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_agent
from app.routers import assist as assist_router

pytestmark = pytest.mark.asyncio


def _row(mapping=None, scalar=None, rows=None):
    r = MagicMock()
    r.mappings.return_value.first.return_value = mapping
    r.mappings.return_value.all.return_value = rows if rows is not None else []
    r.scalar.return_value = scalar
    return r


def _sql(db) -> list[str]:
    return [" ".join(str(c.args[0]).split()) for c in db.execute.await_args_list]


# ── D-1: goto un-claims the step being left ──────────────────────────────────

async def test_goto_unclaims_other_presented_steps_before_presenting_the_target():
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[
        _row({"job_id": "j", "status": "active"}),                                   # session
        _row({"step_status": "pending", "node_status": "pending", "title": "T5"}),  # target
        _row(rows=[{"node_key": "T2", "status": "presented"}]),                      # others presented
        _row(),                                                                      # UPDATE others → pending
        _row(),                                                                      # UPDATE target → presented
        _row(),                                                                      # UPDATE cursor
        _row(scalar="## guidance"),                                                  # SELECT guidance
    ])
    db.commit = AsyncMock()
    checks: list = []
    with patch("app.modules.assist_step_fsm.check", side_effect=lambda site, **kw: checks.append((site, kw)) or True):
        out = await assist_agent.goto_step(session_id="s", node_key="T5", db=db)
    assert out["ok"] is True
    sql = _sql(db)
    unclaim = [q for q in sql if "UPDATE assist_steps SET status = 'pending'" in q]
    assert unclaim and "node_key <> :nk" in unclaim[0] and "IN ('presented', 'awaiting_input')" in unclaim[0]
    present = [i for i, q in enumerate(sql) if "status = 'presented'" in q][0]
    assert sql.index(unclaim[0]) < present, "un-claim the old step BEFORE presenting the new one"
    # the oracle saw both moves
    assert ("goto_step", {"src": "presented", "dst": "pending", "node_status": "pending", "node_key": "T2", "trigger": "unclaim"}) in checks
    assert any(s == "goto_step" and kw["trigger"] == "goto" and kw["dst"] == "presented" for s, kw in checks)


async def test_goto_with_no_other_presented_step_writes_nothing_extra():
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[
        _row({"job_id": "j", "status": "active"}),
        _row({"step_status": "presented", "node_status": "pending", "title": "T5"}),
        _row(rows=[]),                       # nobody else presented
        _row(), _row(), _row(scalar=None),
    ])
    db.commit = AsyncMock()
    with patch("app.modules.assist_step_fsm.check", return_value=True):
        assert (await assist_agent.goto_step(session_id="s", node_key="T5", db=db))["ok"] is True
    assert not [q for q in _sql(db) if "SET status = 'pending'" in q]


# ── D-1: reopen un-claims too, both branches ─────────────────────────────────

@pytest.mark.parametrize("preserve", [True, False])
async def test_reopen_unclaims_other_presented_steps(preserve):
    db = MagicMock()
    db.execute = AsyncMock(side_effect=[
        _row(scalar="committed"),                                    # prior status of the reopened step
        _row(rows=[{"node_key": "T9", "status": "awaiting_input"}]),  # others
        _row(),                                                      # UPDATE others
        _row(), _row(), _row(),                                      # dag_nodes, assist_steps, cursor
    ])
    db.commit = AsyncMock()
    checks: list = []
    with patch("app.modules.assist_step_fsm.check", side_effect=lambda site, **kw: checks.append((site, kw)) or True):
        await assist_agent._reopen_step_mirrored(db=db, job_id="j", session_id="s", node_key="T3",
                                                 preserve_guidance=preserve)
    sql = _sql(db)
    assert any("SET status = 'pending'" in q and "node_key <> :nk" in q for q in sql)
    assert ("reopen_step", {"src": "awaiting_input", "dst": "pending", "node_status": "pending", "node_key": "T9", "trigger": "unclaim"}) in checks
    reopen = [kw for s, kw in checks if s == "reopen_step" and kw["node_key"] == "T3"]
    assert reopen and reopen[0]["src"] == "committed" and reopen[0]["trigger"] == "reopen"
    assert reopen[0]["dst"] == ("presented" if preserve else "pending")


# ── D-2: retire ──────────────────────────────────────────────────────────────

def _retire_db(*, prior: str, cursor, next_key="T4"):
    db = MagicMock()
    seq = [
        _row(),                       # UPDATE dag_nodes → done
        _row(scalar=prior),           # prior step status
    ]
    if prior == "pending":
        seq.append(_row())            # claim: pending → presented
    seq += [
        _row(),                       # commit UPDATE
        _row(scalar=cursor),          # SELECT current_node_key
    ]
    seq += [_row()] * 4               # pointer UPDATE (maybe), memory reconcile, …
    db.execute = AsyncMock(side_effect=seq)
    db.commit = AsyncMock()
    return db


async def test_retire_of_the_cursor_step_moves_the_pointer():
    db = _retire_db(prior="presented", cursor="T3")
    with patch.object(assist_router.assist_policy, "is_completion_evidence", return_value=True), \
         patch.object(assist_router.assist_agent, "_next_pending_node_key", AsyncMock(return_value="T4")), \
         patch("app.modules.assist_step_fsm.check", return_value=True):
        ok = await assist_router._retire_step_mirrored(db=db, job_id="j", session_id="s", node_key="T3",
                                                        evidence="done: output attached, it works")
    assert ok is not False
    sql = _sql(db)
    commit = [q for q in sql if "SET status='committed'" in q][0]
    assert "status IN ('presented','awaiting_input')" in commit, "the FSM's commit sources, not NOT IN(terminal)"
    ptr = [q for q in sql if "UPDATE assist_sessions SET current_node_key" in q]
    assert ptr and db.execute.await_args_list[sql.index(ptr[0])].args[1]["nk"] == "T4"


async def test_retire_of_a_non_cursor_step_keeps_the_pointer():
    db = _retire_db(prior="presented", cursor="T7")
    with patch.object(assist_router.assist_policy, "is_completion_evidence", return_value=True), \
         patch.object(assist_router.assist_agent, "_next_pending_node_key", AsyncMock(return_value="T4")) as nxt, \
         patch("app.modules.assist_step_fsm.check", return_value=True):
        await assist_router._retire_step_mirrored(db=db, job_id="j", session_id="s", node_key="T3",
                                                   evidence="done: output attached, it works")
    assert not [q for q in _sql(db) if "UPDATE assist_sessions SET current_node_key" in q], \
        "an out-of-cursor retire must not yank the operator off the step they are on (§17.1103 sibling)"
    nxt.assert_not_awaited()


async def test_retire_of_a_pending_step_presents_it_first_and_runs_the_oracle():
    db = _retire_db(prior="pending", cursor=None)
    checks: list = []
    with patch.object(assist_router.assist_policy, "is_completion_evidence", return_value=True), \
         patch.object(assist_router.assist_agent, "_next_pending_node_key", AsyncMock(return_value="T4")), \
         patch("app.modules.assist_step_fsm.check", side_effect=lambda site, **kw: checks.append((site, kw)) or True):
        await assist_router._retire_step_mirrored(db=db, job_id="j", session_id="s", node_key="T3",
                                                   evidence="done: output attached, it works")
    sql = _sql(db)
    claim = [i for i, q in enumerate(sql) if "SET status='presented'" in q and "status='pending'" in q]
    commit = [i for i, q in enumerate(sql) if "SET status='committed'" in q]
    assert claim and commit and claim[0] < commit[0], "claim (pending → presented) precedes the commit"
    assert [s for s, _ in checks] == ["tracker_retire_claim", "tracker_retire"]
    assert checks[0][1]["trigger"] == "claim" and checks[1][1]["trigger"] == "commit"
    # cursor was empty → pointer set (self-heal)
    assert [q for q in sql if "UPDATE assist_sessions SET current_node_key" in q]
