"""Tests for the §17.131 concurrent-execution guard ergonomics.

Two layers:
  - Unit: ``_orphan_diagnostic`` against a mocked AsyncSession — verifies
    threshold + seconds_until_reap math and the three suggested_action
    branches (wait_for_reaper / call_cleanup_or_wait / wait_or_inspect).
  - Integration: drive ``execute_all_nodes`` to the Session-1 guard
    rejection path and assert the SSE error payload contains the
    enrichment fields.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import execution_agent


# ---------------------------------------------------------------------------
# Unit — _orphan_diagnostic
# ---------------------------------------------------------------------------

def _mock_db_with_rows(rows: list[dict]):
    """Build an AsyncSession whose first execute() returns a result whose
    .mappings() yields the given rows."""
    mapping_result = MagicMock()
    mapping_result.__iter__ = lambda self: iter(rows)
    result = MagicMock()
    result.mappings.return_value = mapping_result
    db = AsyncMock()
    db.execute = AsyncMock(return_value=result)
    return db


@pytest.mark.asyncio
async def test_diagnostic_no_running_nodes_returns_wait_or_inspect():
    db = _mock_db_with_rows([])
    out = await execution_agent._orphan_diagnostic(db, "job-1")
    assert out["running_nodes"] == []
    assert out["oldest_started_at"] is None
    assert out["suggested_action"] == "wait_or_inspect"
    # Threshold + interval surfaced from settings
    assert isinstance(out["node_orphan_threshold_minutes"], int)
    assert isinstance(out["cleanup_interval_seconds"], int)
    assert out["cleanup_endpoint"] == "POST /jobs/cleanup"


@pytest.mark.asyncio
async def test_diagnostic_past_due_node_suggests_wait_for_reaper():
    """A running node with negative seconds_until_reap → reaper will catch
    it on the next cycle → suggest wait."""
    started = datetime(2026, 5, 12, 0, 0, 0, tzinfo=timezone.utc)
    db = _mock_db_with_rows([
        {"node_key": "T1", "started_at": started, "seconds_until_reap": -300.0},
    ])
    out = await execution_agent._orphan_diagnostic(db, "job-1")
    assert out["suggested_action"] == "wait_for_reaper"
    assert out["oldest_started_at"] == started.isoformat()
    assert out["running_nodes"][0]["seconds_until_reap"] == -300
    assert out["running_nodes"][0]["node_key"] == "T1"


@pytest.mark.asyncio
async def test_diagnostic_near_due_node_suggests_call_cleanup_or_wait(monkeypatch):
    """A node with seconds_until_reap within cleanup_interval → reaper
    won't fire in time → user can force-cleanup or wait."""
    monkeypatch.setattr(execution_agent.settings, "cleanup_interval_seconds", 900)
    db = _mock_db_with_rows([
        {"node_key": "T1", "started_at": datetime.now(timezone.utc), "seconds_until_reap": 600.0},
    ])
    out = await execution_agent._orphan_diagnostic(db, "job-1")
    assert out["suggested_action"] == "call_cleanup_or_wait"


@pytest.mark.asyncio
async def test_diagnostic_fresh_node_suggests_wait_or_inspect(monkeypatch):
    """A node started moments ago is probably a legit run — don't push
    the operator toward cleanup."""
    monkeypatch.setattr(execution_agent.settings, "cleanup_interval_seconds", 900)
    monkeypatch.setattr(execution_agent.settings, "node_orphan_threshold_minutes", 30)
    db = _mock_db_with_rows([
        {"node_key": "T1", "started_at": datetime.now(timezone.utc), "seconds_until_reap": 1500.0},
    ])
    out = await execution_agent._orphan_diagnostic(db, "job-1")
    assert out["suggested_action"] == "wait_or_inspect"


@pytest.mark.asyncio
async def test_diagnostic_multiple_nodes_oldest_first():
    """Oldest_started_at is the earliest of the running nodes; rows are
    sorted ASC by the SQL so the first row drives the field."""
    early = datetime(2026, 5, 12, 0, 0, 0, tzinfo=timezone.utc)
    late = early + timedelta(minutes=10)
    # SQL returns ASC, so caller gives them already sorted.
    db = _mock_db_with_rows([
        {"node_key": "T1", "started_at": early, "seconds_until_reap": -60.0},
        {"node_key": "T2", "started_at": late, "seconds_until_reap": 600.0},
    ])
    out = await execution_agent._orphan_diagnostic(db, "job-1")
    assert len(out["running_nodes"]) == 2
    assert out["oldest_started_at"] == early.isoformat()
    # Past-due dominates: at least one negative → wait_for_reaper.
    assert out["suggested_action"] == "wait_for_reaper"


@pytest.mark.asyncio
async def test_diagnostic_sql_is_parameterized():
    """The SQL passes job_id + threshold as bind params, never interpolates."""
    db = _mock_db_with_rows([])
    await execution_agent._orphan_diagnostic(db, "job-xyz")
    call = db.execute.await_args
    sql_obj, params = call.args
    sql_text = str(sql_obj)
    assert "FROM dag_nodes" in sql_text
    assert "status = 'running'" in sql_text
    assert params["jid"] == "job-xyz"
    assert "thresh" in params


# ---------------------------------------------------------------------------
# Integration — execute_all_nodes Session-1 guard rejection enrichment
# ---------------------------------------------------------------------------

async def _collect_sse(generator):
    """Drain an async generator of SSE strings into a list."""
    out = []
    async for chunk in generator:
        out.append(chunk)
    return out


def _parse_sse_data(chunk: str) -> dict:
    """Parse the JSON ``data:`` line out of an SSE chunk."""
    import json
    for line in chunk.splitlines():
        if line.startswith("data: "):
            return json.loads(line.removeprefix("data: "))
    return {}


@pytest.mark.asyncio
async def test_already_executing_409_includes_diagnostic(monkeypatch):
    """Drive the guard to its "Job is already executing" branch and assert
    the enrichment fields land in the SSE error event."""
    monkeypatch.setattr(execution_agent.settings, "node_orphan_threshold_minutes", 30)
    monkeypatch.setattr(execution_agent.settings, "cleanup_interval_seconds", 900)

    # Mock async_session() so the guard's UPDATE returns 0 rows, then the
    # follow-up _get_job returns status='running', then _orphan_diagnostic
    # returns one past-due node.
    guard_update = MagicMock()
    guard_update.rowcount = 0

    job_lookup = MagicMock()
    job_lookup.mappings.return_value.first.return_value = {
        "id": "job-1", "status": "running", "refined_brief": "x",
    }

    started = datetime(2026, 5, 12, 0, 0, 0, tzinfo=timezone.utc)
    diag_rows = MagicMock()
    diag_rows.__iter__ = lambda self: iter([
        {"node_key": "T2", "started_at": started, "seconds_until_reap": -120.0},
    ])
    diag_result = MagicMock()
    diag_result.mappings.return_value = diag_rows

    db = AsyncMock()
    # Three execute() calls in this code path:
    #   1) UPDATE jobs SET status='running' ... (guard)
    #   2) SELECT ... FROM jobs WHERE id=:id (_get_job inside guard branch)
    #   3) SELECT ... FROM dag_nodes ... (_orphan_diagnostic)
    db.execute = AsyncMock(side_effect=[guard_update, job_lookup, diag_result])
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)

    with patch.object(execution_agent, "async_session", return_value=db):
        chunks = await _collect_sse(execution_agent.execute_all_nodes("job-1"))

    # The "queued"/"slot acquired" events may precede error; find the error.
    errors = [c for c in chunks if c.startswith("event: error")]
    assert len(errors) == 1
    payload = _parse_sse_data(errors[0])
    assert payload["message"] == "Job is already executing"
    assert payload["http_status"] == 409
    assert payload["job_id"] == "job-1"
    # Diagnostic enrichment
    assert payload["node_orphan_threshold_minutes"] == 30
    assert payload["cleanup_interval_seconds"] == 900
    assert payload["cleanup_endpoint"] == "POST /jobs/cleanup"
    assert payload["suggested_action"] == "wait_for_reaper"
    assert payload["oldest_started_at"] == started.isoformat()
    assert len(payload["running_nodes"]) == 1
    assert payload["running_nodes"][0]["node_key"] == "T2"


@pytest.mark.asyncio
async def test_diagnostic_failure_falls_back_to_minimal_payload(monkeypatch):
    """A DB error inside _orphan_diagnostic must NOT mask the 409 — the
    error event still goes out, with a sensible minimal diag."""
    monkeypatch.setattr(execution_agent.settings, "node_orphan_threshold_minutes", 30)
    monkeypatch.setattr(execution_agent.settings, "cleanup_interval_seconds", 900)

    guard_update = MagicMock()
    guard_update.rowcount = 0

    job_lookup = MagicMock()
    job_lookup.mappings.return_value.first.return_value = {
        "id": "job-1", "status": "running", "refined_brief": "x",
    }

    db = AsyncMock()
    # Third call (diagnostic) explodes
    db.execute = AsyncMock(side_effect=[
        guard_update, job_lookup, RuntimeError("db kaboom"),
    ])
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)

    with patch.object(execution_agent, "async_session", return_value=db):
        chunks = await _collect_sse(execution_agent.execute_all_nodes("job-1"))

    errors = [c for c in chunks if c.startswith("event: error")]
    assert len(errors) == 1
    payload = _parse_sse_data(errors[0])
    # 409 still surfaced
    assert payload["http_status"] == 409
    assert payload["message"] == "Job is already executing"
    # Fallback diagnostic has the settings constants but empty running_nodes
    assert payload["running_nodes"] == []
    assert payload["oldest_started_at"] is None
    assert payload["suggested_action"] == "wait_or_inspect"
    assert payload["node_orphan_threshold_minutes"] == 30
    assert payload["cleanup_endpoint"] == "POST /jobs/cleanup"


@pytest.mark.asyncio
async def test_already_completed_409_unchanged(monkeypatch):
    """Re-execute on a completed job: that 409 is NOT enriched (the
    operator's recourse is /jobs/{id}/resume style, not /jobs/cleanup)."""
    guard_update = MagicMock()
    guard_update.rowcount = 0

    job_lookup = MagicMock()
    job_lookup.mappings.return_value.first.return_value = {
        "id": "job-1", "status": "completed", "refined_brief": "x",
    }

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[guard_update, job_lookup])
    db.__aenter__ = AsyncMock(return_value=db)
    db.__aexit__ = AsyncMock(return_value=False)

    with patch.object(execution_agent, "async_session", return_value=db):
        chunks = await _collect_sse(execution_agent.execute_all_nodes("job-1"))

    errors = [c for c in chunks if c.startswith("event: error")]
    assert len(errors) == 1
    payload = _parse_sse_data(errors[0])
    assert payload["message"] == "Job already completed; cannot re-execute"
    assert payload["http_status"] == 409
    # No diag enrichment on the completed path
    assert "running_nodes" not in payload
    assert "suggested_action" not in payload
