"""Tests for app.main._pre_migration_sweep (audit item 7).

Idempotent UPDATE that runs on every startup before migrations to clear
any stuck `running` research_sessions older than 30 minutes — so
migration 020's UNIQUE-index precondition is robust regardless of when
020 first applies and regardless of crash-recovery state.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.main import _pre_migration_sweep


def _mock_session_cm(scalar_value, rowcount=0):
    """Build the async-context-manager chain `async_session()` returns,
    with an inner `db.begin()` ALSO an async context manager. Two execute
    calls are expected: existence check (returns scalar_value) then
    UPDATE (returns rowcount)."""
    db = MagicMock()
    db.begin = MagicMock()
    db.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    db.begin.return_value.__aexit__ = AsyncMock(return_value=None)

    exists_result = MagicMock()
    exists_result.scalar = MagicMock(return_value=scalar_value)
    update_result = MagicMock()
    update_result.rowcount = rowcount
    db.execute = AsyncMock(side_effect=[exists_result, update_result])

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=db)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    return session_cm, db


async def test_sweep_skips_when_table_does_not_exist():
    """Fresh DB before migration 010 — research_sessions table doesn't
    exist yet; sweep should report skipped and not attempt the UPDATE."""
    session_cm, db = _mock_session_cm(scalar_value=None)
    with patch("app.main.async_session", return_value=session_cm):
        result = await _pre_migration_sweep()
    assert result == {"skipped": True, "reason": "table_not_yet_created", "cleared": 0}
    # Only the existence check should have run; no UPDATE.
    assert db.execute.await_count == 1


async def test_sweep_runs_update_when_table_exists_with_stuck_rows():
    """Established DB; the UPDATE matches 3 stuck rows."""
    session_cm, db = _mock_session_cm(scalar_value=1, rowcount=3)
    with patch("app.main.async_session", return_value=session_cm):
        result = await _pre_migration_sweep()
    assert result == {"skipped": False, "reason": None, "cleared": 3}
    # Existence check + UPDATE.
    assert db.execute.await_count == 2
    update_sql = db.execute.await_args_list[1].args[0].text
    assert "UPDATE research_sessions" in update_sql
    assert "status = 'cancelled'" in update_sql
    assert "30 minutes" in update_sql


async def test_sweep_idempotent_when_no_stuck_rows():
    """Healthy DB; UPDATE matches zero rows."""
    session_cm, db = _mock_session_cm(scalar_value=1, rowcount=0)
    with patch("app.main.async_session", return_value=session_cm):
        result = await _pre_migration_sweep()
    assert result == {"skipped": False, "reason": None, "cleared": 0}


async def test_sweep_handles_none_rowcount():
    """Some asyncpg paths return rowcount=None; treat as 0."""
    session_cm, db = _mock_session_cm(scalar_value=1, rowcount=None)
    with patch("app.main.async_session", return_value=session_cm):
        result = await _pre_migration_sweep()
    assert result["cleared"] == 0
