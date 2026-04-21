"""Tests for app/modules/execution_handler.py (#9.29)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.modules import execution_handler


def _row(**kw):
    return SimpleNamespace(**kw)


def _mock_db(job_row, node_rows):
    """Return a db whose two sequential execute() calls return (job, nodes)."""
    job_result = MagicMock()
    job_result.fetchone.return_value = job_row
    nodes_result = MagicMock()
    nodes_result.fetchall.return_value = node_rows
    db = AsyncMock()
    db.execute.side_effect = [job_result, nodes_result]
    return db


async def test_execution_status_returns_error_when_job_missing():
    job_result = MagicMock()
    job_result.fetchone.return_value = None
    db = AsyncMock()
    db.execute.return_value = job_result
    result = await execution_handler.execution_status(uuid4(), db)
    assert "error" in result


async def test_execution_status_identifies_next_pending_with_deps_met():
    job = _row(id="j1", title="t", status="executing", compiled_output=None)
    nodes = [
        _row(node_key="T1", title="First", status="done", execution_order=1,
             depends_on=[], assigned_model=None),
        _row(node_key="T2", title="Second", status="pending", execution_order=2,
             depends_on=["T1"], assigned_model=None),
        _row(node_key="T3", title="Third", status="pending", execution_order=3,
             depends_on=["T2"], assigned_model=None),
    ]
    db = _mock_db(job, nodes)
    result = await execution_handler.execution_status(uuid4(), db)
    # T2's deps are met (T1 is done); T3's aren't (T2 is pending)
    assert result["next_node"]["node_key"] == "T2"


async def test_skipped_counts_as_satisfied_for_deps(regression_check=True):
    """#7.6 — a skipped upstream shouldn't lock downstream."""
    job = _row(id="j1", title="t", status="executing", compiled_output=None)
    nodes = [
        _row(node_key="T1", title="", status="skipped", execution_order=1,
             depends_on=[], assigned_model=None),
        _row(node_key="T2", title="", status="pending", execution_order=2,
             depends_on=["T1"], assigned_model=None),
    ]
    db = _mock_db(job, nodes)
    result = await execution_handler.execution_status(uuid4(), db)
    assert result["next_node"]["node_key"] == "T2"


async def test_failed_node_is_not_actionable(regression_check=True):
    """#7.7 — failed nodes require /exec/retry, not picked up by /execute."""
    job = _row(id="j1", title="t", status="executing", compiled_output=None)
    nodes = [
        _row(node_key="T1", title="", status="failed", execution_order=1,
             depends_on=[], assigned_model=None),
    ]
    db = _mock_db(job, nodes)
    result = await execution_handler.execution_status(uuid4(), db)
    # Failed node must NOT be the next_node
    assert result["next_node"] is None
    assert result["nodes"][0]["actionable"] is False


async def test_status_counts_by_state():
    job = _row(id="j1", title="t", status="running", compiled_output=None)
    nodes = [
        _row(node_key=f"T{i}", title="", status=status, execution_order=i,
             depends_on=[], assigned_model=None)
        for i, status in enumerate(
            ["done", "done", "pending", "pending", "failed", "skipped"]
        )
    ]
    db = _mock_db(job, nodes)
    result = await execution_handler.execution_status(uuid4(), db)
    assert result["counts"] == {"done": 2, "pending": 2, "failed": 1, "skipped": 1}
    assert result["total_nodes"] == 6
