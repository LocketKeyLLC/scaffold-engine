"""§17.281 — auto-complete fires only when every node is in {done, skipped}.

Pre-fix bug: ``execute_next_node``'s post-verify autocomplete path used
``SELECT COUNT(*) ... WHERE status = 'pending'`` and flipped the job to
``completed`` on COUNT==0. That missed surviving ``failed`` / ``blocked``
/ ``running`` nodes, so a DAG that finished with any unresolved failure
could mis-finalize.

Post-fix: the path uses :func:`_all_nodes_done` (``NOT IN ('done',
'skipped')``) shared with the L644 autocomplete path, so any non-success
status defeats the flip.
"""
import re
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules.execution_agent import _all_nodes_done


def _make_count_db(count: int) -> AsyncMock:
    """db.execute(...).scalar() returns ``count``."""
    db = AsyncMock()
    result = MagicMock()
    result.scalar.return_value = count
    db.execute.return_value = result
    return db


@pytest.mark.smoke
class TestAllNodesDoneAutocompleteSemantics:
    """§17.281 — the autocomplete gate, the bug pinned at the helper level."""

    async def test_zero_unfinished_returns_true(self):
        """All nodes done/skipped → COUNT==0 → helper True → autocomplete fires."""
        db = _make_count_db(0)
        assert await _all_nodes_done(db, "job-1") is True

    async def test_failed_node_present_returns_false(self):
        """The §17.281 bug: a surviving ``failed`` node must defeat autocomplete.

        Pre-fix, the inline ``status = 'pending'`` count returned 0 here
        and let the job flip to ``completed``. Post-fix, the helper sees
        the failed row via ``NOT IN ('done','skipped')`` and returns False.
        """
        db = _make_count_db(1)
        assert await _all_nodes_done(db, "job-1") is False

    async def test_blocked_node_present_returns_false(self):
        """``blocked`` is also non-success terminal — must defeat autocomplete."""
        db = _make_count_db(2)
        assert await _all_nodes_done(db, "job-1") is False

    async def test_running_node_present_returns_false(self):
        """Concurrent execution race — another node mid-run defers autocomplete
        to the call that finishes it.
        """
        db = _make_count_db(1)
        assert await _all_nodes_done(db, "job-1") is False


# §17.281 — exact SQL shape that produced the bug. Other legitimate uses
# of ``status = 'pending'`` in the file (the ``_get_next_node`` claim,
# retry cascade) do not match this full pattern.
_BUGGY_AUTOCOMPLETE_SQL = re.compile(
    r"SELECT\s+COUNT\(\*\)\s+FROM\s+dag_nodes\s+WHERE\s+job_id\s*=\s*:jid\s+AND\s+status\s*=\s*'pending'",
    re.IGNORECASE,
)


@pytest.mark.smoke
class TestAllNodesDoneRegressionGuard:
    """§17.281 — source-shape guard so a future refactor cannot silently
    restore the pre-fix inline count.
    """

    def test_pre_fix_buggy_sql_is_absent(self):
        from app.modules import execution_agent

        with open(execution_agent.__file__, encoding="utf-8") as f:
            src = f.read()

        assert _BUGGY_AUTOCOMPLETE_SQL.search(src) is None, (
            "§17.281 regression: the autocomplete gate using "
            "`SELECT COUNT(*) FROM dag_nodes WHERE job_id = :jid AND status = 'pending'` "
            "has reappeared. This pattern misses 'failed' / 'blocked' / 'running' "
            "nodes and lets a job with unresolved failures flip to 'completed'. "
            "Use `_all_nodes_done(db, job_id)` instead — it shares the gate with "
            "the L644 autocomplete path."
        )

    def test_all_nodes_done_helper_uses_not_in_done_skipped(self):
        """The helper's anchor — anything other than done/skipped counts."""
        from app.modules import execution_agent

        with open(execution_agent.__file__, encoding="utf-8") as f:
            src = f.read()

        assert "NOT IN ('done', 'skipped')" in src, (
            "§17.281: `_all_nodes_done` must exclude every non-success status "
            "via `NOT IN ('done', 'skipped')`. A drift to a narrower predicate "
            "(e.g. only excluding 'pending') would silently re-introduce the "
            "auto-complete bug."
        )


# ---------------------------------------------------------------------------
# §17.854 (audit A1/A5) — guarded job-status flip helpers: never resurrect a
# terminal job, never block a job with a still-running node.
# ---------------------------------------------------------------------------

def _make_flip_db(returned_id):
    """db.execute(...).fetchone() → returned_id (a row or None); records SQL."""
    db = AsyncMock()
    result = MagicMock()
    result.fetchone.return_value = returned_id
    db.execute.return_value = result
    db.commit = AsyncMock()
    return db, result


@pytest.mark.smoke
class TestGuardedJobFlips:
    async def test_flip_completed_returns_true_when_row_flipped(self):
        from app.modules.execution_agent import _flip_job_completed
        db, _ = _make_flip_db(("job-1",))
        assert await _flip_job_completed(db, "job-1") is True

    async def test_flip_completed_returns_false_when_guard_blocks(self):
        """A terminal job (cancelled/failed) → UPDATE matches 0 rows → False."""
        from app.modules.execution_agent import _flip_job_completed
        db, _ = _make_flip_db(None)
        assert await _flip_job_completed(db, "job-1") is False

    async def test_flip_completed_sql_excludes_terminal_states(self):
        """The predicate must exclude cancelled+failed so an orphaned executor
        cannot resurrect a cancelled job to 'completed' (the A1 bug)."""
        from app.modules.execution_agent import _flip_job_completed
        db, _ = _make_flip_db(None)
        await _flip_job_completed(db, "job-1")
        sql = str(db.execute.call_args.args[0])
        assert "cancelled" in sql and "failed" in sql
        assert "status != 'completed'" not in sql  # the old, unsafe predicate

    async def test_flip_blocked_sql_guards_running_node(self):
        """The blocked flip must not fire while a sibling node is 'running'
        (the A5 concurrent-execute bug)."""
        from app.modules.execution_agent import _flip_job_blocked
        db, _ = _make_flip_db(None)
        await _flip_job_blocked(db, "job-1")
        sql = str(db.execute.call_args.args[0])
        assert "NOT EXISTS" in sql and "status = 'running'" in sql
        assert "cancelled" in sql and "failed" in sql
