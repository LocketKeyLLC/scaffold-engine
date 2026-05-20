"""Tests for app.main._pre_migration_sweep — startup crash-recovery.

Two-stage sweep:

1. **research_sessions** — cancel any 'running' row older than 5 min
   (audit item 7 / migration 020 precondition).
2. **dag_nodes** — reset any 'running' row to 'pending' and refresh
   ``updated_at`` on the owning jobs (X.25; closes the 30-min
   restart-mid-DAG dead window where ``_REAP_RUNNING_SQL`` refuses to
   fail jobs with running nodes).

Both stages are independent and idempotent on a healthy DB. The legacy
three-key shape (``skipped``/``reason``/``cleared``) describes stage 1;
two additive keys (``dag_nodes_reset``/``parent_jobs_refreshed``)
describe stage 2.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.main import _pre_migration_sweep


def _node_row(job_id="00000000-0000-0000-0000-000000000001"):
    row = MagicMock()
    row.job_id = job_id
    return row


def _mock_session_cm(
    *,
    sessions_exist: bool,
    sessions_rowcount: int = 0,
    nodes_exist: bool = True,
    node_rows: list | None = None,
    refreshed_rows: int = 0,
):
    """Build the async-context-manager chain ``async_session()`` returns,
    with an inner ``db.begin()`` ALSO an async context manager.

    Execute call order in the new two-stage sweep:
      1. existence check for ``research_sessions``
      2. (if exists) UPDATE research_sessions
      3. existence check for ``dag_nodes``
      4. (if exists) UPDATE dag_nodes RETURNING job_id
      5. (if any rows reset) UPDATE jobs (parent refresh) RETURNING id
    """
    db = MagicMock()
    db.begin = MagicMock()
    db.begin.return_value.__aenter__ = AsyncMock(return_value=None)
    db.begin.return_value.__aexit__ = AsyncMock(return_value=None)

    side_effects: list = []

    sessions_exists_result = MagicMock()
    sessions_exists_result.scalar = MagicMock(
        return_value=1 if sessions_exist else None
    )
    side_effects.append(sessions_exists_result)
    if sessions_exist:
        sessions_update_result = MagicMock()
        sessions_update_result.rowcount = sessions_rowcount
        side_effects.append(sessions_update_result)

    nodes_exists_result = MagicMock()
    nodes_exists_result.scalar = MagicMock(
        return_value=1 if nodes_exist else None
    )
    side_effects.append(nodes_exists_result)
    if nodes_exist:
        nodes_update_result = MagicMock()
        nodes_update_result.fetchall = MagicMock(return_value=node_rows or [])
        side_effects.append(nodes_update_result)
        if node_rows:
            refresh_result = MagicMock()
            refresh_result.fetchall = MagicMock(
                return_value=[object()] * refreshed_rows
            )
            side_effects.append(refresh_result)

    db.execute = AsyncMock(side_effect=side_effects)

    session_cm = MagicMock()
    session_cm.__aenter__ = AsyncMock(return_value=db)
    session_cm.__aexit__ = AsyncMock(return_value=None)
    return session_cm, db


async def test_sweep_skips_when_research_sessions_table_does_not_exist():
    """Fresh DB before migration 010 — research_sessions table doesn't
    exist yet; stage 1 reports skipped. Stage 2 still runs against
    dag_nodes (which exists in the init.sql baseline)."""
    session_cm, db = _mock_session_cm(sessions_exist=False, nodes_exist=True)
    with patch("app.main.async_session", return_value=session_cm):
        result = await _pre_migration_sweep()
    assert result["skipped"] is True
    assert result["reason"] == "table_not_yet_created"
    assert result["cleared"] == 0
    assert result["dag_nodes_reset"] == 0
    assert result["parent_jobs_refreshed"] == 0
    # research_sessions existence check + dag_nodes existence check + dag_nodes UPDATE.
    assert db.execute.await_count == 3


async def test_sweep_skips_both_when_neither_table_exists():
    """Truly fresh DB — neither table created yet."""
    session_cm, db = _mock_session_cm(sessions_exist=False, nodes_exist=False)
    with patch("app.main.async_session", return_value=session_cm):
        result = await _pre_migration_sweep()
    assert result == {
        "skipped": True,
        "reason": "table_not_yet_created",
        "cleared": 0,
        "dag_nodes_reset": 0,
        "parent_jobs_refreshed": 0,
    }
    assert db.execute.await_count == 2  # two existence checks, nothing else


async def test_sweep_runs_update_when_table_exists_with_stuck_rows():
    """Established DB; the research_sessions UPDATE matches 3 stuck rows."""
    session_cm, db = _mock_session_cm(
        sessions_exist=True, sessions_rowcount=3, nodes_exist=True,
    )
    with patch("app.main.async_session", return_value=session_cm):
        result = await _pre_migration_sweep()
    assert result["skipped"] is False
    assert result["reason"] is None
    assert result["cleared"] == 3
    # Existence + UPDATE for research_sessions, existence + UPDATE for dag_nodes.
    assert db.execute.await_count == 4
    sessions_update_sql = db.execute.await_args_list[1].args[0].text
    assert "UPDATE research_sessions" in sessions_update_sql
    assert "status = 'cancelled'" in sessions_update_sql
    # §17.198: cutoff is now driven by settings.startup_sweep_research_
    # idle_min (default 5) via a bind param. Verify both the SQL shape
    # and that the bind value matches the live settings value.
    assert "make_interval(mins => :idle_min)" in sessions_update_sql
    from app.config import settings
    sessions_update_params = db.execute.await_args_list[1].args[1]
    assert sessions_update_params == {
        "idle_min": settings.startup_sweep_research_idle_min,
    }


async def test_sweep_idempotent_when_no_stuck_rows():
    """Healthy DB; both UPDATEs match zero rows."""
    session_cm, db = _mock_session_cm(
        sessions_exist=True, sessions_rowcount=0, nodes_exist=True,
    )
    with patch("app.main.async_session", return_value=session_cm):
        result = await _pre_migration_sweep()
    assert result == {
        "skipped": False,
        "reason": None,
        "cleared": 0,
        "dag_nodes_reset": 0,
        "parent_jobs_refreshed": 0,
    }


async def test_sweep_handles_none_rowcount():
    """Some asyncpg paths return rowcount=None for the research_sessions
    UPDATE; treat as 0."""
    session_cm, db = _mock_session_cm(
        sessions_exist=True, sessions_rowcount=None, nodes_exist=True,
    )
    with patch("app.main.async_session", return_value=session_cm):
        result = await _pre_migration_sweep()
    assert result["cleared"] == 0


async def test_sweep_resets_orphan_dag_nodes_at_startup():
    """X.25: stage 2 — running dag_nodes are reset to 'pending' with no
    time threshold (at startup the executor is dead by definition)."""
    rows = [_node_row("00000000-0000-0000-0000-000000000001"),
            _node_row("00000000-0000-0000-0000-000000000001"),
            _node_row("00000000-0000-0000-0000-000000000002")]
    session_cm, db = _mock_session_cm(
        sessions_exist=True, sessions_rowcount=0,
        nodes_exist=True, node_rows=rows, refreshed_rows=2,
    )
    with patch("app.main.async_session", return_value=session_cm):
        result = await _pre_migration_sweep()
    assert result["dag_nodes_reset"] == 3
    assert result["parent_jobs_refreshed"] == 2

    # Stage 2 SQL: the dag_nodes UPDATE must NOT have a time threshold —
    # at startup all 'running' rows are crash-orphans by construction.
    nodes_update_sql = db.execute.await_args_list[3].args[0].text
    assert "UPDATE dag_nodes" in nodes_update_sql
    assert "status = 'pending'" in nodes_update_sql
    assert "started_at" not in nodes_update_sql
    assert "INTERVAL" not in nodes_update_sql
    assert "make_interval" not in nodes_update_sql

    # Parent-jobs refresh fires only when nodes were reset, and only
    # touches jobs in 'running'/'executing' (so a job that already
    # finalized to a terminal status is not perturbed).
    refresh_sql = db.execute.await_args_list[4].args[0].text
    assert "UPDATE jobs" in refresh_sql
    assert "'running'" in refresh_sql and "'executing'" in refresh_sql


async def test_sweep_skips_parent_refresh_when_no_orphan_nodes():
    """No running dag_nodes → no refresh UPDATE fires (4 statements total)."""
    session_cm, db = _mock_session_cm(
        sessions_exist=True, sessions_rowcount=0,
        nodes_exist=True, node_rows=[],
    )
    with patch("app.main.async_session", return_value=session_cm):
        result = await _pre_migration_sweep()
    assert result["dag_nodes_reset"] == 0
    assert result["parent_jobs_refreshed"] == 0
    assert db.execute.await_count == 4


async def test_sweep_skips_dag_nodes_when_table_missing():
    """research_sessions exists but dag_nodes doesn't (theoretical
    out-of-order migration scenario). Stage 2 short-circuits cleanly."""
    session_cm, db = _mock_session_cm(
        sessions_exist=True, sessions_rowcount=0, nodes_exist=False,
    )
    with patch("app.main.async_session", return_value=session_cm):
        result = await _pre_migration_sweep()
    assert result["dag_nodes_reset"] == 0
    assert result["parent_jobs_refreshed"] == 0
    # research_sessions existence + UPDATE + dag_nodes existence; nothing more.
    assert db.execute.await_count == 3
