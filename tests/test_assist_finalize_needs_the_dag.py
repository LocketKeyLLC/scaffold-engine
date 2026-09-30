"""§17.1208 — the session being finished is not the job being finished.

`_maybe_finalize_session` counts `assist_steps`, and `handed_off` is terminal
there: it means the operator gave the step to the autonomous executor, not that
the work succeeded. Whether it did lives in `dag_nodes`.

Live, on the operator's job — and the two columns line up exactly:

    assist_steps:  61 committed · 47 skipped · 23 handed_off
    dag_nodes:     61 done      · 47 skipped · 21 pending · 2 failed

The 23 handed-off steps ARE the 23 unfinished nodes. The job was marked
`completed`, the header badge said Done, and the deliverable opened with
"✅ Completed via Assist Mode" — over 21 steps that never ran and 2 that failed.

The autonomous path has guarded this since §17.281 (`_all_nodes_done`), written
because a DAG finishing with any surviving failure still flipped to completed.
The assist path simply never asked.
"""
from __future__ import annotations

import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.execution_compile import _prepend_assist_completed_banner as banner


# ── the deliverable must not call an unfinished job Completed ─────────────

def test_a_finished_job_still_reads_as_completed():
    out = banner("BODY", 61)
    assert "✅ **Completed via Assist Mode**" in out
    assert "you executed and verified 61 steps" in out


def test_an_unfinished_job_says_what_is_left():
    """The step count was never the lie — 61 steps really were executed. The
    word "Completed" was."""
    out = banner("BODY", 61, unfinished=23, failed=2)
    assert "⚠️ **Partly done via Assist Mode**" in out
    assert "Completed via Assist Mode" not in out
    assert "you executed and verified 61 steps" in out, "the work done is still stated"
    assert "23 steps did not finish (2 failed)" in out
    assert "not the whole plan" in out


def test_unfinished_without_failures_does_not_invent_them():
    out = banner("BODY", 5, unfinished=3, failed=0)
    assert "3 steps did not finish." in out and "failed" not in out


def test_one_step_reads_as_one_step():
    assert "1 step did not finish" in banner("B", 2, unfinished=1)


def test_no_body_is_left_alone():
    assert banner(None, 61, unfinished=23) is None


def test_the_count_comes_from_the_same_nodes_the_deliverable_is_built_from():
    """Counting from anywhere else is how the banner and the body drift."""
    import app.modules.execution_compile as ec
    src = inspect.getsource(ec._compile_output)
    blk = src[src.index("if assist_completed:"):src.index("_prepend_plan_only_banner")]
    assert '_unfinished = [n for n in nodes if n.get("status") not in ("done", "skipped")]' in blk, blk
    assert 'failed=sum(1 for n in _unfinished if n.get("status") == "failed")' in blk, blk


# ── the job status itself ────────────────────────────────────────────────

def _db(incomplete: int, unfinished_nodes: int):
    """A db whose scalar() answers the two COUNT(*) queries in order: the
    session's non-terminal steps, then the DAG's non-terminal nodes."""
    seen: list[str] = []
    scalars = iter([incomplete, unfinished_nodes])

    async def execute(stmt, params=None):
        sql = " ".join(str(stmt).split())
        seen.append(sql)
        res = MagicMock()
        res.scalar = lambda: next(scalars, 0)
        res.mappings.return_value.first.return_value = {"job_id": "J1"}
        return res

    db = MagicMock()
    db.execute = AsyncMock(side_effect=execute)
    db.commit = AsyncMock()
    db.seen = seen
    return db


def _job_completed_sql(db) -> list[str]:
    return [s for s in db.seen if "UPDATE jobs" in s and "status = 'completed'" in s]


@pytest.mark.asyncio
async def test_the_job_is_not_completed_while_the_dag_has_work_left():
    """The operator's exact case: every step terminal (23 of them handed_off),
    23 nodes not."""
    from app.modules import assist_agent as aa
    db = _db(incomplete=0, unfinished_nodes=23)
    with patch("app.modules.assist_agent.savepoint", side_effect=AssertionError("must not get that far")):
        await aa._maybe_finalize_session(session_id="S1", db=db)
    assert _job_completed_sql(db) == [], "a job with 23 unfinished nodes was marked completed"


@pytest.mark.asyncio
async def test_the_session_is_still_finalized_so_it_does_not_keep_asking():
    """Its steps ARE all terminal — there is nothing left to ask the operator
    in-session. Only the JOB's status is withheld."""
    from app.modules import assist_agent as aa
    db = _db(incomplete=0, unfinished_nodes=23)
    with patch("app.modules.assist_agent.savepoint", side_effect=AssertionError("stop here")):
        await aa._maybe_finalize_session(session_id="S1", db=db)
    assert any("UPDATE assist_sessions" in s and "status = 'completed'" in s for s in db.seen), db.seen


@pytest.mark.asyncio
async def test_a_genuinely_finished_job_still_completes():
    """The guard must not block the normal path, or assist jobs never finish."""
    from app.modules import assist_agent as aa
    db = _db(incomplete=0, unfinished_nodes=0)
    with patch("app.modules.assist_agent.savepoint", side_effect=RuntimeError("stop after the flip")):
        try:
            await aa._maybe_finalize_session(session_id="S1", db=db)
        except RuntimeError:
            pass
    assert _job_completed_sql(db), "a DAG with nothing outstanding must still complete"


@pytest.mark.asyncio
async def test_an_unfinished_session_returns_before_anything():
    from app.modules import assist_agent as aa
    db = _db(incomplete=3, unfinished_nodes=0)
    await aa._maybe_finalize_session(session_id="S1", db=db)
    assert not any("UPDATE assist_sessions" in s for s in db.seen)


def test_the_guard_reuses_the_autonomous_rule():
    """One definition of "the DAG is done", or the two paths disagree about
    what finished means — which is how this defect existed at all."""
    from app.modules import assist_agent as aa
    src = inspect.getsource(aa._maybe_finalize_session)
    assert "from app.modules.execution_agent import _all_nodes_done" in src, src
    assert "dag_done = await _all_nodes_done(db, str(sess[\"job_id\"]))" in src, src
    assert src.index("dag_done") < src.index("UPDATE jobs"), "the gate must precede the flip"


def test_handed_off_is_still_terminal_for_the_session():
    """Not a regression guard on the fix — a guard on the READING of it. The
    fix must not make `handed_off` non-terminal, which would leave sessions
    open forever waiting on steps the executor owns."""
    from app.modules import assist_agent as aa
    src = inspect.getsource(aa._maybe_finalize_session)
    blk = src[:src.index("if incomplete")]
    assert "'committed', 'skipped', 'handed_off', 'escalated'" in blk, blk
