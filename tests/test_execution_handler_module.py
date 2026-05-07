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


# ---------------------------------------------------------------------------
# next_actions integration (audit item 10)
# ---------------------------------------------------------------------------


async def test_next_actions_populated_for_failed_status():
    """When the job is 'failed' the response surfaces retry/skip/delete
    options with the actual failed node_key substituted."""
    job_id = uuid4()
    job = _row(id="j1", title="t", status="failed", compiled_output=None)
    nodes = [
        _row(node_key="T1", title="", status="done", execution_order=1,
             depends_on=[], assigned_model=None),
        _row(node_key="T2", title="", status="failed", execution_order=2,
             depends_on=["T1"], assigned_model=None),
    ]
    db = _mock_db(job, nodes)
    result = await execution_handler.execution_status(job_id, db)
    actions = result["next_actions"]
    kinds = {a["action"] for a in actions}
    assert {"retry_node", "skip_node", "delete"} <= kinds
    retry = next(a for a in actions if a["action"] == "retry_node")
    # job_id and the failed node_key are filled in.
    assert str(job_id) in retry["command"]
    assert " T2" in retry["command"]


async def test_next_actions_for_awaiting_confirmation():
    """An awaiting-confirmation job offers /confirm + delete."""
    job_id = uuid4()
    job = _row(id="j1", title="t", status="awaiting_confirmation",
               compiled_output=None)
    db = _mock_db(job, [])
    result = await execution_handler.execution_status(job_id, db)
    kinds = {a["action"] for a in result["next_actions"]}
    assert {"confirm", "delete"} <= kinds


async def test_next_actions_for_completed_renders_view_output():
    job_id = uuid4()
    job = _row(id="j1", title="t", status="completed", compiled_output="out")
    nodes = [
        _row(node_key="T1", title="", status="done", execution_order=1,
             depends_on=[], assigned_model=None),
    ]
    db = _mock_db(job, nodes)
    result = await execution_handler.execution_status(job_id, db)
    actions = result["next_actions"]
    assert len(actions) == 1
    assert actions[0]["action"] == "view_output"


async def test_next_actions_blocked_node_picks_correct_node_key():
    """Pending node whose deps aren't met → blocked_node_key surfaces in
    skip suggestions for in-flight jobs."""
    job_id = uuid4()
    job = _row(id="j1", title="t", status="running", compiled_output=None)
    nodes = [
        _row(node_key="T1", title="", status="failed", execution_order=1,
             depends_on=[], assigned_model=None),
        _row(node_key="T2", title="", status="pending", execution_order=2,
             depends_on=["T1"], assigned_model=None),
    ]
    db = _mock_db(job, nodes)
    result = await execution_handler.execution_status(job_id, db)
    # failed_node_key wins precedence over blocked_node_key in the
    # registry helper, so retry/skip should reference T1 (the failed one).
    skip = next(
        (a for a in result["next_actions"] if a["action"] == "skip_node"),
        None,
    )
    assert skip is not None
    assert " T1" in skip["command"]
