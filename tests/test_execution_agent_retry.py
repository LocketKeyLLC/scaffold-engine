"""Unit tests for `retry_failed_node` — the `/exec/retry` request path.

Coverage gap closed: pre-2026-05-08 the function had zero direct tests.
This file walks every branch:

  * Stage 1 validation: not-found, wrong-status, exhausted retries.
  * Stages 2-4 topology: BFS reaches transitive downstream nodes.
  * Stage 5 reset selection: only `pending`/`failed` downstream get
    flipped to `pending` — `done`/`skipped` are preserved (i.e. a
    successful sibling subgraph isn't invalidated when an upstream
    sibling is retried).
  * Stage 5 atomic UPDATEs: target node + downstream + jobs row.
  * Stage 7 return shape: status='reset', incremented retry_count,
    downstream_reset list.

Concurrency / partial-failure paths are out of scope here — they need
real Postgres and live in `tests/integration/`. This file uses the
AsyncMock DB pattern so the suite stays unit-test fast.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules.execution_agent import retry_failed_node


# ---------------------------------------------------------------------------
# Result-shape helpers — emulate sqlalchemy result objects with .fetchone()
# returning a Row-like with attribute access, .fetchall() returning a list.
# ---------------------------------------------------------------------------


def _row(**fields):
    """Build a Row-like object with attribute access via MagicMock(spec=)."""
    r = MagicMock()
    for k, v in fields.items():
        setattr(r, k, v)
    return r


def _result_one(row):
    r = MagicMock()
    r.fetchone.return_value = row
    return r


def _result_all(rows):
    r = MagicMock()
    r.fetchall.return_value = rows
    return r


def _result_update():
    """Result for an UPDATE — only rowcount matters here, default to 1."""
    r = MagicMock()
    r.rowcount = 1
    return r


def _build_db(*, target_row, all_rows):
    """Build an AsyncMock db whose .execute() returns results in the order
    retry_failed_node makes calls:
      1. SELECT target node row
      2. SELECT all topology rows
      3. UPDATE target node
      4. UPDATE downstream nodes (only if downstream_to_reset non-empty)
      5. UPDATE jobs row (always fires)
    Any extra UPDATEs hit the default _result_update().
    """
    db = AsyncMock()
    db.commit = AsyncMock()
    db.execute = AsyncMock(side_effect=_make_execute_side_effect(
        target_row=target_row, all_rows=all_rows,
    ))
    return db


def _make_execute_side_effect(*, target_row, all_rows):
    queue = [
        _result_one(target_row),
        _result_all(all_rows),
    ]
    # Subsequent calls (UPDATEs) — make a generator that always returns
    # _result_update() so the order doesn't matter (some tests have an
    # optional downstream UPDATE).
    def _side_effect(*args, **kwargs):
        if queue:
            return queue.pop(0)
        return _result_update()
    return _side_effect


@pytest.mark.smoke
class TestRetryFailedNodeValidation:
    """Stage 1 — refuse to retry when the row state forbids it."""

    async def test_node_not_found_returns_error(self):
        """Missing row → error dict, no DB writes."""
        db = _build_db(target_row=None, all_rows=[])
        result = await retry_failed_node("job-1", "T1", db)

        assert result["status"] == "error"
        assert "not found" in result["message"].lower()
        # Only one execute (the SELECT) — no UPDATE attempted.
        assert db.execute.call_count == 1
        db.commit.assert_not_called()

    async def test_non_failed_status_returns_error(self):
        """Refuse to retry a 'running' or 'done' node."""
        target = _row(node_key="T1", status="running", retry_count=0, max_retries=3)
        db = _build_db(target_row=target, all_rows=[])
        result = await retry_failed_node("job-1", "T1", db)

        assert result["status"] == "error"
        assert "'running', not 'failed'" in result["message"]
        assert db.execute.call_count == 1
        db.commit.assert_not_called()

    async def test_exhausted_retries_returns_error(self):
        """retry_count >= max_retries → error, no reset."""
        target = _row(node_key="T1", status="failed", retry_count=3, max_retries=3)
        db = _build_db(target_row=target, all_rows=[])
        result = await retry_failed_node("job-1", "T1", db)

        assert result["status"] == "error"
        assert "exhausted retries" in result["message"]
        assert "3/3" in result["message"]
        db.commit.assert_not_called()

    async def test_retry_count_one_under_max_is_allowed(self):
        """Boundary: retry_count == max_retries-1 must succeed (the next
        attempt would be the last allowed)."""
        target = _row(node_key="T1", status="failed", retry_count=2, max_retries=3)
        db = _build_db(target_row=target, all_rows=[
            _row(node_key="T1", status="failed", depends_on=[]),
        ])
        result = await retry_failed_node("job-1", "T1", db)

        assert result["status"] == "reset"
        assert result["retry_count"] == 3


@pytest.mark.smoke
class TestRetryFailedNodeLeafCase:
    """Stage 5 — failed leaf node (no dependents): reset target + jobs."""

    async def test_leaf_returns_status_reset_with_incremented_retry(self):
        target = _row(node_key="T1", status="failed", retry_count=0, max_retries=3)
        all_rows = [_row(node_key="T1", status="failed", depends_on=[])]
        db = _build_db(target_row=target, all_rows=all_rows)

        result = await retry_failed_node("job-1", "T1", db)

        assert result == {
            "status": "reset",
            "node_key": "T1",
            "retry_count": 1,
            "downstream_reset": [],
        }
        # SELECT (target) + SELECT (all) + UPDATE (target node) + UPDATE (jobs)
        # = 4 calls. No downstream UPDATE because downstream_reset is empty.
        assert db.execute.call_count == 4
        db.commit.assert_called_once()


@pytest.mark.smoke
class TestRetryFailedNodeDownstreamReset:
    """Stages 3-5 — BFS over dependents and selective reset."""

    async def test_pending_and_failed_downstream_reset_done_preserved(self):
        """Critical contract: a successful sibling subgraph is preserved.

        Topology:  T1 → T2 → T3
                       ↓
                       T4 (done)
        Retry T1: T2 + T3 must reset (pending), T4 must NOT (done).
        """
        target = _row(node_key="T1", status="failed", retry_count=0, max_retries=3)
        all_rows = [
            _row(node_key="T1", status="failed",  depends_on=[]),
            _row(node_key="T2", status="pending", depends_on=["T1"]),
            _row(node_key="T3", status="failed",  depends_on=["T2"]),
            _row(node_key="T4", status="done",    depends_on=["T1"]),
        ]
        db = _build_db(target_row=target, all_rows=all_rows)

        result = await retry_failed_node("job-1", "T1", db)

        assert result["status"] == "reset"
        assert result["retry_count"] == 1
        assert sorted(result["downstream_reset"]) == ["T2", "T3"]
        # SELECT × 2 + UPDATE target + UPDATE downstream + UPDATE jobs = 5
        assert db.execute.call_count == 5

    async def test_skipped_downstream_preserved(self):
        """`skipped` is a terminal-by-choice state. Retrying an upstream
        must not undo that operator decision."""
        target = _row(node_key="T1", status="failed", retry_count=1, max_retries=5)
        all_rows = [
            _row(node_key="T1", status="failed",  depends_on=[]),
            _row(node_key="T2", status="skipped", depends_on=["T1"]),
        ]
        db = _build_db(target_row=target, all_rows=all_rows)

        result = await retry_failed_node("job-1", "T1", db)

        assert result["downstream_reset"] == []
        # No downstream UPDATE — skipped excluded; SELECT×2 + UPDATE target
        # + UPDATE jobs = 4
        assert db.execute.call_count == 4

    async def test_transitive_dependents_reached_via_bfs(self):
        """Diamond: T1 → {T2, T3}; both → T4.
        Retry T1 with T2/T3/T4 all failed → all three reset."""
        target = _row(node_key="T1", status="failed", retry_count=0, max_retries=3)
        all_rows = [
            _row(node_key="T1", status="failed", depends_on=[]),
            _row(node_key="T2", status="failed", depends_on=["T1"]),
            _row(node_key="T3", status="failed", depends_on=["T1"]),
            _row(node_key="T4", status="failed", depends_on=["T2", "T3"]),
        ]
        db = _build_db(target_row=target, all_rows=all_rows)

        result = await retry_failed_node("job-1", "T1", db)

        assert sorted(result["downstream_reset"]) == ["T2", "T3", "T4"]

    async def test_unrelated_failed_branch_not_reset(self):
        """Failed nodes in a sibling branch (not transitive dependents)
        must stay failed — retrying T1 doesn't fix T9."""
        target = _row(node_key="T1", status="failed", retry_count=0, max_retries=3)
        all_rows = [
            _row(node_key="T1", status="failed", depends_on=[]),
            _row(node_key="T2", status="failed", depends_on=["T1"]),
            _row(node_key="T9", status="failed", depends_on=[]),  # unrelated
        ]
        db = _build_db(target_row=target, all_rows=all_rows)

        result = await retry_failed_node("job-1", "T1", db)

        assert sorted(result["downstream_reset"]) == ["T2"]
        assert "T9" not in result["downstream_reset"]


@pytest.mark.smoke
class TestRetryFailedNodeReturnShape:
    """Stage 7 — caller contract."""

    async def test_returns_status_reset_keyword(self):
        """Pipeline + handler render different UX based on this string."""
        target = _row(node_key="T1", status="failed", retry_count=0, max_retries=3)
        all_rows = [_row(node_key="T1", status="failed", depends_on=[])]
        db = _build_db(target_row=target, all_rows=all_rows)
        result = await retry_failed_node("job-1", "T1", db)
        assert result["status"] == "reset"
        # The four contracted keys.
        assert set(result.keys()) == {
            "status", "node_key", "retry_count", "downstream_reset",
        }
