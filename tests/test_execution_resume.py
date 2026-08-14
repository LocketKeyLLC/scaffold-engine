"""Tests for app.modules.execution_resume — §17.774 crash-resume.

``resume_orphaned_executions`` runs once at lifespan startup and, for every
job left ``running``/``executing`` by a process crash:

  * resumes it (flip to ``executing`` + spawn a detached ``execute_all_nodes``
    drain) when the crash-loop guard still has budget, OR
  * fails it (``error_summary='crash_resume_budget_exhausted'``) when a restart
    made no new progress and the ``resume_attempts`` streak would exceed
    ``settings.execution_max_resume_attempts``.

These unit tests mock the async session (same pattern as
test_pre_migration_sweep) and drive the counter logic with ``spawn=False`` so no
real DAG runs; one test flips ``spawn=True`` to assert the drain is scheduled.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import app.modules.execution_resume as er
from app.modules.execution_resume import (
    CRASH_RESUME_BUDGET_SUMMARY,
    resume_orphaned_executions,
)


def _result_candidates(rows: list[dict]):
    r = MagicMock()
    r.mappings.return_value.all.return_value = rows
    return r


def _result_scalar(value):
    r = MagicMock()
    r.scalar.return_value = value
    return r


def _result_first(row):
    r = MagicMock()
    r.first.return_value = row
    return r


def _mock_session(side_effects: list):
    """Build the ``async_session()`` context manager whose ``db.execute``
    yields ``side_effects`` in order. ``db.commit`` is an awaitable no-op."""
    db = MagicMock()
    db.execute = AsyncMock(side_effect=side_effects)
    db.commit = AsyncMock()
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=db)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm, db


JID = "00000000-0000-0000-0000-0000000000aa"


async def test_disabled_valve_is_a_noop():
    with patch.object(er.settings, "execution_resume_on_startup_enabled", False):
        # async_session must never be entered when the valve is off.
        with patch("app.modules.execution_resume.async_session") as sess:
            result = await resume_orphaned_executions()
    assert result["skipped"] is True
    assert result["reason"] == "disabled"
    assert result["resumed"] == [] and result["budget_failed"] == []
    sess.assert_not_called()


async def test_no_candidates_is_clean():
    cm, db = _mock_session([_result_candidates([])])
    with patch.object(er.settings, "execution_resume_on_startup_enabled", True), \
         patch("app.modules.execution_resume.async_session", return_value=cm):
        result = await resume_orphaned_executions(spawn=False)
    assert result == {
        "skipped": False, "reason": None,
        "candidates": 0, "resumed": [], "budget_failed": [],
    }
    db.commit.assert_awaited()


async def test_first_resume_claims_and_records_marker():
    """A fresh crash-orphan (attempts=0, marker=0) with 2 done nodes resumes
    as attempt 1 and stamps the progress marker to the current done-count."""
    candidate = {"id": JID, "resume_attempts": 0, "resume_done_marker": 0}
    side = [
        _result_candidates([candidate]),
        _result_scalar(2),            # 2 done nodes
        _result_first(object()),      # claim UPDATE matched
    ]
    cm, db = _mock_session(side)
    with patch.object(er.settings, "execution_resume_on_startup_enabled", True), \
         patch.object(er.settings, "execution_max_resume_attempts", 3), \
         patch("app.modules.execution_resume.async_session", return_value=cm):
        result = await resume_orphaned_executions(spawn=False)

    assert result["resumed"] == [JID]
    assert result["budget_failed"] == []

    claim_sql = db.execute.await_args_list[2].args[0].text
    claim_params = db.execute.await_args_list[2].args[1]
    assert "status = 'executing'" in claim_sql          # off 'running' so the guard accepts
    assert claim_params == {"jid": JID, "attempts": 1, "marker": 2}


async def test_zero_progress_increments_streak():
    """attempts=1, marker=2, still 2 done nodes → no progress → attempt 2."""
    candidate = {"id": JID, "resume_attempts": 1, "resume_done_marker": 2}
    side = [
        _result_candidates([candidate]),
        _result_scalar(2),            # same done-count as marker → no progress
        _result_first(object()),
    ]
    cm, db = _mock_session(side)
    with patch.object(er.settings, "execution_resume_on_startup_enabled", True), \
         patch.object(er.settings, "execution_max_resume_attempts", 3), \
         patch("app.modules.execution_resume.async_session", return_value=cm):
        result = await resume_orphaned_executions(spawn=False)
    assert result["resumed"] == [JID]
    assert db.execute.await_args_list[2].args[1] == {"jid": JID, "attempts": 2, "marker": 2}


async def test_progress_resets_streak_to_one():
    """A high streak but new done nodes since the marker → reset to attempt 1."""
    candidate = {"id": JID, "resume_attempts": 2, "resume_done_marker": 2}
    side = [
        _result_candidates([candidate]),
        _result_scalar(4),            # 4 > marker 2 → progress
        _result_first(object()),
    ]
    cm, db = _mock_session(side)
    with patch.object(er.settings, "execution_resume_on_startup_enabled", True), \
         patch.object(er.settings, "execution_max_resume_attempts", 3), \
         patch("app.modules.execution_resume.async_session", return_value=cm):
        result = await resume_orphaned_executions(spawn=False)
    assert result["resumed"] == [JID]
    assert db.execute.await_args_list[2].args[1] == {"jid": JID, "attempts": 1, "marker": 4}


async def test_budget_exhausted_fails_instead_of_relaunching():
    """attempts=3, no progress → would be attempt 4 > cap 3 → fail, no claim."""
    candidate = {"id": JID, "resume_attempts": 3, "resume_done_marker": 1}
    side = [
        _result_candidates([candidate]),
        _result_scalar(1),            # still 1 done node → no progress
        _result_first(object()),      # fail UPDATE matched
    ]
    cm, db = _mock_session(side)
    with patch.object(er.settings, "execution_resume_on_startup_enabled", True), \
         patch.object(er.settings, "execution_max_resume_attempts", 3), \
         patch("app.modules.execution_resume.async_session", return_value=cm):
        result = await resume_orphaned_executions(spawn=False)

    assert result["resumed"] == []
    assert result["budget_failed"] == [JID]
    fail_sql = db.execute.await_args_list[2].args[0].text
    fail_params = db.execute.await_args_list[2].args[1]
    assert "status = 'failed'" in fail_sql
    assert fail_params == {"jid": JID, "summary": CRASH_RESUME_BUDGET_SUMMARY}


async def test_spawn_schedules_drain_for_resumed_jobs():
    candidate = {"id": JID, "resume_attempts": 0, "resume_done_marker": 0}
    side = [
        _result_candidates([candidate]),
        _result_scalar(0),
        _result_first(object()),
    ]
    cm, db = _mock_session(side)
    with patch.object(er.settings, "execution_resume_on_startup_enabled", True), \
         patch.object(er.settings, "execution_max_resume_attempts", 3), \
         patch("app.modules.execution_resume.async_session", return_value=cm), \
         patch("app.modules.execution_resume._spawn_resume_drain") as spawn:
        result = await resume_orphaned_executions(spawn=True)
    assert result["resumed"] == [JID]
    spawn.assert_called_once_with(JID)


def test_recovery_hint_for_budget_exhausted():
    """A crash_resume_budget_exhausted job (status 'failed') surfaces the
    node-retry action tagged with the reason_kind."""
    from app.modules.recovery import next_actions_for

    actions = next_actions_for(
        "failed", JID,
        failed_node_key="T3",
        error_summary=CRASH_RESUME_BUDGET_SUMMARY,
    )
    top = actions[0]
    assert top["reason_kind"] == "crash_resume_budget"
    assert top["action"] == "retry_node"
    assert top["command"] == f"/exec retry {JID} T3"
