"""Tests for execution_agent — execute_all_nodes SSE streaming, blocked, abnormal exit, completed guard.

Split from the original test_execution_agent.py (#9.6). Shared imports
and helpers live in _execution_agent_shared.
"""
from tests._execution_agent_shared import *  # noqa: F401, F403

@pytest.mark.smoke
class TestExecuteAllNodesSSESequence:
    """SSE events emitted in correct order for a 2-node happy path."""

    async def test_happy_path_event_order(self):
        """node_start → node_done → node_start → node_done → pipeline_complete"""
        db, mock_session = _make_sse_db(dag_node_count=2)

        mock_get_job = AsyncMock(return_value={
            "status": "executing", "id": "job-1"
        })
        mock_get_next = AsyncMock(side_effect=[
            {"node_key": "T1", "title": "Research", "tool": "SearXNG"},
            {"node_key": "T2", "title": "Summarize", "tool": "LLM"},
            None,  # no more pending nodes
        ])
        mock_exec_next = AsyncMock(side_effect=[
            {"status": "done", "node_key": "T1", "title": "Research",
             "output": "results", "verified": True, "confidence": 0.95,
             "model_used": "qwen2.5:7b"},
            {"status": "done", "node_key": "T2", "title": "Summarize",
             "output": "summary", "verified": True, "confidence": 0.90,
             "model_used": "qwen2.5:7b"},
            {"status": "complete", "message": "All nodes done."},
        ])

        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._peek_next_node", mock_get_next), \
             patch("app.modules.execution_agent.execute_next_node", mock_exec_next):
            from app.modules.execution_agent import execute_all_nodes
            events = await _collect_sse(execute_all_nodes("job-1"))

        event_names = [e[0] for e in events]
        assert event_names == [
            "node_start", "node_done",
            "node_start", "node_done",
            "pipeline_complete",
        ]

    async def test_pipeline_complete_has_summary_fields(self):
        """pipeline_complete event contains total_nodes, passed, failed, duration_ms."""
        db, mock_session = _make_sse_db(dag_node_count=1)

        mock_get_job = AsyncMock(return_value={"status": "executing", "id": "job-1"})
        mock_get_next = AsyncMock(side_effect=[
            {"node_key": "T1", "title": "Task", "tool": "LLM"},
            None,
        ])
        mock_exec_next = AsyncMock(side_effect=[
            {"status": "done", "node_key": "T1", "title": "Task",
             "output": "out", "verified": True, "confidence": 0.9,
             "model_used": "qwen2.5:7b"},
            {"status": "complete"},
        ])

        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._peek_next_node", mock_get_next), \
             patch("app.modules.execution_agent.execute_next_node", mock_exec_next):
            from app.modules.execution_agent import execute_all_nodes
            events = await _collect_sse(execute_all_nodes("job-1"))

        complete_evt = [e for e in events if e[0] == "pipeline_complete"][0][1]
        assert "total_nodes" in complete_evt
        assert "passed" in complete_evt
        assert "failed" in complete_evt
        assert "duration_ms" in complete_evt
        assert complete_evt["passed"] == 1
        assert complete_evt["failed"] == 0

    async def test_node_start_includes_tool(self):
        """node_start event contains node_key, title, and tool."""
        db, mock_session = _make_sse_db(dag_node_count=1)

        mock_get_job = AsyncMock(return_value={"status": "executing", "id": "job-1"})
        mock_get_next = AsyncMock(side_effect=[
            {"node_key": "T1", "title": "KB Lookup", "tool": "Milvus"},
            None,
        ])
        mock_exec_next = AsyncMock(side_effect=[
            {"status": "done", "node_key": "T1", "title": "KB Lookup",
             "output": "data", "verified": True, "confidence": 0.9,
             "model_used": "qwen2.5:7b"},
            {"status": "complete"},
        ])

        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._peek_next_node", mock_get_next), \
             patch("app.modules.execution_agent.execute_next_node", mock_exec_next):
            from app.modules.execution_agent import execute_all_nodes
            events = await _collect_sse(execute_all_nodes("job-1"))

        start_evt = events[0]
        assert start_evt[0] == "node_start"
        assert start_evt[1]["node_key"] == "T1"
        assert start_evt[1]["tool"] == "Milvus"


@pytest.mark.smoke
class TestExecuteAllNodesBlocked:
    """When T1 fails, downstream blocked nodes emit blocked SSE event."""

    async def test_blocked_event_on_upstream_failure(self):
        """T1 fails → execute_next_node returns blocked with blocked_nodes payload."""
        db, mock_session = _make_sse_db(dag_node_count=2)

        mock_get_job = AsyncMock(return_value={"status": "executing", "id": "job-1"})
        mock_get_next = AsyncMock(side_effect=[
            {"node_key": "T1", "title": "Research", "tool": "LLM"},
            None,  # T2 is blocked, no next node
        ])
        mock_exec_next = AsyncMock(side_effect=[
            {"status": "failed", "node_key": "T1", "title": "Research",
             "error": "LLM timeout", "model_used": "qwen2.5:7b"},
            {"status": "blocked", "message": "Nodes blocked",
             "blocked_nodes": [
                 {"node_key": "T2", "title": "Summarize",
                  "depends_on": ["T1"],
                  "blocked_by": [{"node_key": "T1", "status": "failed"}]}
             ]},
        ])

        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._peek_next_node", mock_get_next), \
             patch("app.modules.execution_agent.execute_next_node", mock_exec_next):
            from app.modules.execution_agent import execute_all_nodes
            events = await _collect_sse(execute_all_nodes("job-1"))

        event_names = [e[0] for e in events]
        assert "node_start" in event_names
        assert "node_failed" in event_names
        assert "blocked" in event_names

        blocked_evt = [e for e in events if e[0] == "blocked"][0][1]
        assert "blocked_nodes" in blocked_evt
        assert blocked_evt["blocked_nodes"][0]["node_key"] == "T2"
        assert blocked_evt["blocked_nodes"][0]["blocked_by"][0]["node_key"] == "T1"

    async def test_invalid_job_returns_error_event(self):
        """Non-existent job emits a single error SSE event."""
        db, mock_session = _make_sse_db_guard_fails()
        mock_get_job = AsyncMock(return_value=None)
        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job", mock_get_job):
            from app.modules.execution_agent import execute_all_nodes
            events = await _collect_sse(execute_all_nodes("bad-id"))

        assert len(events) == 1
        assert events[0][0] == "error"
        assert "not found" in events[0][1]["message"]


@pytest.mark.smoke
class TestExecuteAllNodesAbnormalExit:
    """#2: try/finally transitions stuck 'running' jobs to terminal state."""

    async def test_exception_mid_loop_transitions_to_failed(self):
        """RuntimeError from execute_next_node → finally marks job 'failed' + emits execution_failed SSE."""
        guard_result = MagicMock(); guard_result.rowcount = 1
        dag_check = MagicMock(); dag_check.scalar.return_value = 1
        # §17.624 — the hands-on gate reads the DAG's tool tags after DAG-gen;
        # one LLM node → not hands-on → gate doesn't park, loop runs as before.
        dag_tools = MagicMock()
        dag_tools.mappings.return_value.all.return_value = [{"tool": "LLM"}]
        cleanup_status = MagicMock(); cleanup_status.scalar.return_value = "running"
        cleanup_update = MagicMock()

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            guard_result,     # Session 1 guard UPDATE
            dag_check,        # Session 3 DAG COUNT
            dag_tools,        # §17.624 hands-on gate: SELECT tool FROM dag_nodes
            cleanup_status,   # finally: SELECT status
            cleanup_update,   # finally: UPDATE status='failed'
        ])
        db.commit = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock(return_value=mock_session_ctx)

        mock_get_job = AsyncMock(return_value={"status": "executing", "id": "job-1"})
        mock_get_next = AsyncMock(return_value={"node_key": "T1", "title": "T", "tool": "LLM"})
        mock_exec_next = AsyncMock(side_effect=RuntimeError("boom"))

        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._peek_next_node", mock_get_next), \
             patch("app.modules.execution_agent.execute_next_node", mock_exec_next):
            from app.modules.execution_agent import execute_all_nodes, drain_cleanup_tasks
            events = await _collect_sse(execute_all_nodes("job-1"))
            # X.24: cleanup runs as a detached task — wait for it before
            # asserting on the mocked DB.
            await drain_cleanup_tasks()

        event_names = [e[0] for e in events]
        assert "execution_failed" in event_names, f"execution_failed missing from {event_names}"

        # Verify the cleanup UPDATE was called with 'failed'
        update_calls = [
            c for c in db.execute.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], dict)
               and c.args[1].get("s") == "failed"
        ]
        assert len(update_calls) >= 1, "Cleanup UPDATE with status='failed' not found"

    async def test_cancelled_error_transitions_to_cancelled_and_reraises(self):
        """CancelledError from execute_next_node → finally marks 'cancelled' + re-raises."""
        guard_result = MagicMock(); guard_result.rowcount = 1
        dag_check = MagicMock(); dag_check.scalar.return_value = 1
        # §17.624 — hands-on gate reads tool tags; one LLM node → not hands-on.
        dag_tools = MagicMock()
        dag_tools.mappings.return_value.all.return_value = [{"tool": "LLM"}]
        cleanup_status = MagicMock(); cleanup_status.scalar.return_value = "running"
        cleanup_update = MagicMock()

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            guard_result, dag_check, dag_tools, cleanup_status, cleanup_update,
        ])
        db.commit = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock(return_value=mock_session_ctx)

        mock_get_job = AsyncMock(return_value={"status": "executing", "id": "job-1"})
        mock_get_next = AsyncMock(return_value={"node_key": "T1", "title": "T", "tool": "LLM"})
        mock_exec_next = AsyncMock(side_effect=asyncio.CancelledError())

        async def _run_collecting():
            events = []
            reraised = False
            with patch("app.modules.execution_agent.async_session", mock_session), \
                 patch("app.modules.execution_agent._get_job", mock_get_job), \
                 patch("app.modules.execution_agent._peek_next_node", mock_get_next), \
                 patch("app.modules.execution_agent.execute_next_node", mock_exec_next):
                from app.modules.execution_agent import execute_all_nodes, drain_cleanup_tasks
                try:
                    async for chunk in execute_all_nodes("job-1"):
                        events.append(chunk)
                except asyncio.CancelledError:
                    reraised = True
                # X.24: cleanup runs as a detached task — wait for it
                # under the patch so the mocked DB sees the UPDATE.
                await drain_cleanup_tasks()
            return events, reraised

        _, reraised = await _run_collecting()
        assert reraised, "CancelledError was not re-raised after cleanup"

        update_calls = [
            c for c in db.execute.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], dict)
               and c.args[1].get("s") == "cancelled"
        ]
        assert len(update_calls) >= 1, "Cleanup UPDATE with status='cancelled' not found"

    async def test_clean_exit_does_not_double_write_status(self):
        """Normal completion: finally sees status != 'running', does NOT UPDATE again."""
        guard_result = MagicMock(); guard_result.rowcount = 1
        dag_check = MagicMock(); dag_check.scalar.return_value = 1
        # execute_next_node (mocked) returns status=complete → _build_pipeline_summary
        # runs a SELECT compiled_output, returns empty. Then finally's SELECT status
        # returns 'completed' (clean exit already transitioned via execute_next_node).
        co_select = MagicMock(); co_select.scalar.return_value = ""
        cleanup_status_completed = MagicMock()
        cleanup_status_completed.scalar.return_value = "completed"

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            guard_result,            # guard
            dag_check,               # DAG count
            co_select,               # _build_pipeline_summary compiled_output SELECT
            cleanup_status_completed # finally SELECT status='completed' -> no-op
        ])
        db.commit = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock(return_value=mock_session_ctx)

        mock_get_job = AsyncMock(return_value={"status": "executing", "id": "job-1"})
        mock_get_next = AsyncMock(return_value=None)
        mock_exec_next = AsyncMock(return_value={"status": "complete"})

        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._peek_next_node", mock_get_next), \
             patch("app.modules.execution_agent.execute_next_node", mock_exec_next):
            from app.modules.execution_agent import execute_all_nodes
            events = await _collect_sse(execute_all_nodes("job-1"))

        # pipeline_complete should fire; no 'failed'/'cancelled' status UPDATE in finally
        terminal_updates = [
            c for c in db.execute.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], dict)
               and c.args[1].get("s") in ("failed", "cancelled")
        ]
        assert len(terminal_updates) == 0, "Finally double-wrote status on clean exit"


@pytest.mark.smoke
class TestExecuteAllNodesCompletedGuard:
    """#17: guard rejects already-completed jobs with 409."""

    async def test_completed_job_returns_409(self):
        guard_result = MagicMock(); guard_result.rowcount = 0  # guard blocks
        db = AsyncMock()
        db.execute = AsyncMock(return_value=guard_result)
        db.commit = AsyncMock()

        mock_session_ctx = AsyncMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=db)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_session = MagicMock(return_value=mock_session_ctx)

        mock_get_job = AsyncMock(return_value={"status": "completed", "id": "job-1"})

        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job", mock_get_job):
            from app.modules.execution_agent import execute_all_nodes
            events = await _collect_sse(execute_all_nodes("job-1"))

        assert len(events) == 1
        assert events[0][0] == "error"
        assert events[0][1].get("http_status") == 409
        assert "completed" in events[0][1]["message"].lower()
