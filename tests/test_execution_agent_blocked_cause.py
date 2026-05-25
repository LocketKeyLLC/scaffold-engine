"""§17.295 — blocked-nodes response splits "failed upstream" from "waiting".

§17.280-UX-9 audit-tail concern: when ``execute_next_node`` returns
``{"status": "blocked", ...}``, the pre-§17.295 shape was

    {"status": "blocked",
     "message": "No executable nodes — dependencies not satisfied",
     "blocked_nodes": [
         {"node_key": "T2", "title": "...", "blocked_by": ["T1"]},
     ]}

with three operator-facing gaps:

  1. ``blocked_by`` carried only node_keys — the caller couldn't tell
     what kind of blocker each dep was.
  2. ONLY pending nodes whose deps included a `failed` upstream
     appeared. Pending nodes blocked solely by `pending` / `running`
     upstream were silently dropped — operators couldn't see the
     wait state.
  3. The top-level ``message`` was generic — same string whether the
     blockage was retryable (failed upstream) or just slow (waiting
     upstream). Different remediations, identical surface.

§17.295 fixes all three:

  * Each ``blocked_nodes`` entry carries ``blocked_by:
    [{node_key, status}, ...]`` (status enum from dag_nodes) AND a
    ``cause: "failed" | "waiting"`` precedence tag.
  * Pending-blocked-by-pending nodes are now included (with cause=
    "waiting") so the operator sees the full wait state.
  * Top-level ``actionable_count`` + ``waiting_count`` summarize the
    split; ``message`` is cause-aware ("X need action, Y waiting").

These tests pin both the precedence logic and the response shape.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.execution_agent import execute_next_node


def _row(node_key: str, status: str, depends_on: list[str], title: str = "") -> MagicMock:
    """Build a dag_nodes row mock supporting attribute access (r.node_key)."""
    r = MagicMock()
    r.node_key = node_key
    r.title = title or node_key
    r.status = status
    r.depends_on = depends_on
    return r


def _make_blocked_branch_db(rows: list) -> AsyncMock:
    """db.execute side_effect for the blocked-branch flow:

      1. _get_job — SELECT jobs
      2. _get_next_node — returns None (mocked separately via patch)
      3. _all_nodes_done — returns False (mocked separately)
      4. SELECT compiled_output (partial-compile cache check)
      5. UPDATE jobs SET status='blocked' (no compiled_output)
      6. SELECT node_key, title, status, depends_on FROM dag_nodes
         (this is where the §17.295 logic runs over `rows`)
    """
    # The §17.295 query returns rows via .fetchall()
    blocked_rows_result = MagicMock()
    blocked_rows_result.fetchall.return_value = rows
    # Partial-compile cache: scalar() returns None → falls to recompute path
    cached_result = MagicMock()
    cached_result.scalar.return_value = None
    update_result = MagicMock()

    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        cached_result,         # SELECT compiled_output
        update_result,         # UPDATE jobs SET ... status='blocked'
        blocked_rows_result,   # SELECT dag_nodes for blocked-node detail
    ])
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
class TestBlockedCausePrecedence:
    """§17.295 — the cause-classification logic."""

    async def _drive_to_blocked_branch(self, rows: list) -> dict:
        """Drive execute_next_node into the blocked branch with `rows`
        as the dag_nodes population."""
        db = _make_blocked_branch_db(rows)
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=db)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        mock_session_factory = MagicMock(return_value=mock_session)

        with patch("app.modules.execution_agent.async_session", mock_session_factory), \
             patch("app.modules.execution_agent._get_job",
                   new_callable=AsyncMock,
                   return_value={"id": "job-1", "status": "running"}), \
             patch("app.modules.execution_agent._get_next_node",
                   new_callable=AsyncMock, return_value=None), \
             patch("app.modules.execution_agent._all_nodes_done",
                   new_callable=AsyncMock, return_value=False), \
             patch("app.modules.execution_agent._compile_output",
                   new_callable=AsyncMock, return_value=(None, False)):
            return await execute_next_node("job-1")

    async def test_failed_upstream_marks_cause_failed(self):
        """Pending node with one failed dep → cause="failed",
        actionable_count=1."""
        rows = [
            _row("T1", "failed", []),
            _row("T2", "pending", ["T1"], title="downstream"),
        ]
        out = await self._drive_to_blocked_branch(rows)
        assert out["status"] == "blocked"
        assert len(out["blocked_nodes"]) == 1
        b = out["blocked_nodes"][0]
        assert b["node_key"] == "T2"
        assert b["cause"] == "failed"
        assert b["blocked_by"] == [{"node_key": "T1", "status": "failed"}]
        assert out["actionable_count"] == 1
        assert out["waiting_count"] == 0

    async def test_pending_upstream_marks_cause_waiting(self):
        """§17.295 load-bearing case: pre-§17.295 these nodes were
        DROPPED from the response entirely. Operator now sees them
        with cause="waiting"."""
        rows = [
            _row("T1", "running", []),
            _row("T2", "pending", ["T1"]),
        ]
        out = await self._drive_to_blocked_branch(rows)
        assert len(out["blocked_nodes"]) == 1
        b = out["blocked_nodes"][0]
        assert b["cause"] == "waiting"
        assert b["blocked_by"] == [{"node_key": "T1", "status": "running"}]
        assert out["actionable_count"] == 0
        assert out["waiting_count"] == 1

    async def test_failed_precedence_wins_over_waiting(self):
        """When a pending node has BOTH a failed dep AND a pending dep,
        cause is "failed" — the failure is the actionable blocker; the
        pending dep behind it won't run anyway."""
        rows = [
            _row("T1", "failed", []),
            _row("T2", "pending", []),  # T2 is also a dep of T3
            _row("T3", "pending", ["T1", "T2"], title="downstream"),
        ]
        out = await self._drive_to_blocked_branch(rows)
        # T2 is pending with no deps — it shouldn't appear (no blocker).
        # T3 has failed+pending deps → cause="failed".
        t3_entry = next(b for b in out["blocked_nodes"] if b["node_key"] == "T3")
        assert t3_entry["cause"] == "failed"
        # Both deps appear in blocked_by with their statuses.
        statuses = {d["status"] for d in t3_entry["blocked_by"]}
        assert statuses == {"failed", "pending"}
        # actionable_count counts T3 once (failed precedence).
        assert out["actionable_count"] == 1

    async def test_done_deps_dont_block(self):
        """Deps in `done` / `skipped` status are NOT blockers — they're
        success-terminal. A pending node whose deps are all done is
        not in blocked_nodes (and wouldn't reach this branch anyway —
        _get_next_node would have returned it)."""
        rows = [
            _row("T1", "done", []),
            _row("T2", "skipped", []),
            _row("T3", "pending", ["T1", "T2"]),
        ]
        out = await self._drive_to_blocked_branch(rows)
        assert out["blocked_nodes"] == []
        assert out["actionable_count"] == 0
        assert out["waiting_count"] == 0

    async def test_blocked_dep_treated_as_failed_cause(self):
        """A `blocked` upstream (transitive failure from further up the
        DAG) is treated the same as `failed` — operator still needs to
        retry / skip somewhere upstream."""
        rows = [
            _row("T1", "blocked", []),
            _row("T2", "pending", ["T1"]),
        ]
        out = await self._drive_to_blocked_branch(rows)
        assert out["blocked_nodes"][0]["cause"] == "failed"
        assert out["actionable_count"] == 1


@pytest.mark.asyncio
class TestBlockedTopLevelMessage:
    """§17.295 — cause-aware top-level message."""

    async def _drive(self, rows: list) -> dict:
        db = _make_blocked_branch_db(rows)
        mock_session = MagicMock()
        mock_session.__aenter__ = AsyncMock(return_value=db)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        with patch("app.modules.execution_agent.async_session",
                   MagicMock(return_value=mock_session)), \
             patch("app.modules.execution_agent._get_job",
                   new_callable=AsyncMock,
                   return_value={"id": "job-1", "status": "running"}), \
             patch("app.modules.execution_agent._get_next_node",
                   new_callable=AsyncMock, return_value=None), \
             patch("app.modules.execution_agent._all_nodes_done",
                   new_callable=AsyncMock, return_value=False), \
             patch("app.modules.execution_agent._compile_output",
                   new_callable=AsyncMock, return_value=(None, False)):
            return await execute_next_node("job-1")

    async def test_message_only_failed(self):
        rows = [_row("T1", "failed", []), _row("T2", "pending", ["T1"])]
        out = await self._drive(rows)
        assert "need action" not in out["message"]
        assert "failed upstream" in out["message"]
        # Surfaces the retry command.
        assert "/exec retry" in out["message"]

    async def test_message_only_waiting(self):
        rows = [_row("T1", "running", []), _row("T2", "pending", ["T1"])]
        out = await self._drive(rows)
        assert "waiting" in out["message"]
        # No retry hint when nothing needs action.
        assert "/exec retry" not in out["message"]

    async def test_message_split_when_both(self):
        """Mixed cause counts → the message reports both buckets."""
        rows = [
            _row("T1", "failed", []),
            _row("T2", "pending", ["T1"]),       # cause=failed
            _row("T3", "running", []),
            _row("T4", "pending", ["T3"]),       # cause=waiting
        ]
        out = await self._drive(rows)
        assert "need action" in out["message"]
        assert "waiting" in out["message"]
        assert out["actionable_count"] == 1
        assert out["waiting_count"] == 1


@pytest.mark.smoke
class TestSourceShapeRegressionGuard:
    """§17.295 — anchor production source against drive-by reverts to
    the pre-§17.295 single-line `blocked_by = [k for k in deps]` shape.
    """

    def test_blocked_by_carries_dict_objects(self):
        from app.modules import execution_agent

        with open(execution_agent.__file__, encoding="utf-8") as f:
            src = f.read()

        # The §17.295 shape uses `{"node_key": k, "status": ...}` —
        # the load-bearing source anchor for the structured dep list.
        assert '"node_key": k, "status":' in src, (
            "§17.295 regression: `blocked_by` is no longer a list of "
            "`{node_key, status}` objects. Operators can't tell failed "
            "from waiting deps without the per-dep status."
        )

    def test_cause_precedence_anchor(self):
        from app.modules import execution_agent

        with open(execution_agent.__file__, encoding="utf-8") as f:
            src = f.read()

        # The precedence-classifying check. A drive-by simplification
        # that drops the {failed, blocked} intersection would silently
        # mis-classify transitive failures as "waiting".
        assert 'dep_statuses & {"failed", "blocked"}' in src, (
            "§17.295 regression: the cause precedence check is gone. "
            "Without `dep_statuses & {failed, blocked}`, a node whose "
            "only blocker is `blocked` (transitive failure) gets "
            "mis-tagged as `waiting`, sending the operator down the "
            "wrong remediation path."
        )

    def test_pipeline_renderer_reads_cause_field(self):
        from pipelines import scaffold_router

        with open(scaffold_router.__file__, encoding="utf-8") as f:
            src = f.read()

        assert 'b.get("cause") == "failed"' in src, (
            "§17.295 regression: the chat-side blocked-event render "
            "no longer splits by cause. The unified pre-§17.295 line "
            "(`(waiting on: ...)`) lumped failed-upstream and waiting-"
            "upstream into one message — different remediations under "
            "one ambiguous label, which is the audit gap UX-9 closes."
        )
