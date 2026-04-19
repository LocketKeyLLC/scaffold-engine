"""
test_execution_agent.py — Unit tests for execution_agent module.

Phase 1: _compile_output() 3-strategy priority chain + partial compile.
Run:  docker exec scaffold-orchestrator pytest tests/test_execution_agent.py -m smoke --timeout=30 -v
"""
import httpx
import pytest
import asyncio
from tests.conftest import make_mock_db


def _run(coro):
    """Run async coroutine synchronously (matches existing test pattern)."""
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.mark.smoke
class TestCompileOutputStrategy1:
    """Strategy 1: output-titled node gets priority."""

    def test_output_node_preferred(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research topic", "tool": "SearXNG",
             "status": "done", "output_text": "search results here"},
            {"node_key": "T2", "title": "Summarize output", "tool": "LLM",
             "status": "done", "output_text": "final summary"},
            {"node_key": "T3", "title": "Review", "tool": "LLM",
             "status": "done", "output_text": "looks good"},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert result == "final summary"

    def test_output_node_case_insensitive(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Final OUTPUT Document", "tool": "LLM",
             "status": "done", "output_text": "the deliverable"},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert result == "the deliverable"

    def test_output_node_not_done_skipped(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Compile output", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T2", "title": "Research", "tool": "LLM",
             "status": "done", "output_text": "fallback content"},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert "fallback content" in result

    def test_output_node_none_text_returns_empty(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Generate output", "tool": "LLM",
             "status": "done", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert result == ""


@pytest.mark.smoke
class TestCompileOutputStrategy2:
    """Strategy 2: last CodeGen terminal node is the deliverable."""

    def test_last_codegen_selected(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Plan", "tool": "LLM",
             "status": "done", "output_text": "plan text"},
            {"node_key": "T2", "title": "Implement", "tool": "CodeGen",
             "status": "done", "output_text": "def hello(): pass"},
            {"node_key": "T3", "title": "Refactor", "tool": "CodeGen",
             "status": "done", "output_text": "def hello():\n    print('hi')"},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert result == "def hello():\n    print('hi')"

    def test_codegen_not_last_falls_through(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Implement", "tool": "CodeGen",
             "status": "done", "output_text": "code here"},
            {"node_key": "T2", "title": "Summarize", "tool": "LLM",
             "status": "done", "output_text": "summary here"},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert "## T1:" in result
        assert "## T2:" in result


@pytest.mark.smoke
class TestCompileOutputStrategy3:
    """Strategy 3: concatenate all passed outputs with headers."""

    def test_concatenation_format(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "SearXNG",
             "status": "done", "output_text": "research data"},
            {"node_key": "T2", "title": "Analyze", "tool": "LLM",
             "status": "done", "output_text": "analysis results"},
            {"node_key": "T3", "title": "Review", "tool": "LLM",
             "status": "done", "output_text": "review notes"},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert "## T1: Research" in result
        assert "## T2: Analyze" in result
        assert "## T3: Review" in result
        assert "---" in result

    def test_failed_nodes_excluded(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "LLM",
             "status": "done", "output_text": "good stuff"},
            {"node_key": "T2", "title": "Analyze", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T3", "title": "Blocked task", "tool": "LLM",
             "status": "blocked", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert "## T1: Research" in result
        assert "T2" not in result
        assert "T3" not in result

    def test_no_done_nodes_returns_empty(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Task A", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T2", "title": "Task B", "tool": "LLM",
             "status": "blocked", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert result == ""


@pytest.mark.smoke
class TestCompileOutputPartial:
    """Partial compile behavior — _compile_output returns what it can;
    caller adds [PARTIAL] prefix."""

    def test_partial_with_some_done(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "Milvus",
             "status": "done", "output_text": "kb results"},
            {"node_key": "T2", "title": "Summarize", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T3", "title": "Compare", "tool": "LLM",
             "status": "blocked", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert "kb results" in result
        assert "T2" not in result
        partial = "[PARTIAL — some nodes failed or blocked]\n\n" + result
        assert partial.startswith("[PARTIAL")

    def test_all_failed_returns_empty_not_none(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Task A", "tool": "LLM",
             "status": "failed", "output_text": None},
            {"node_key": "T2", "title": "Task B", "tool": "LLM",
             "status": "failed", "output_text": None},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert result is not None
        assert isinstance(result, str)
        assert result == ""
# ---------------------------------------------------------------------------
import json
from unittest.mock import patch, AsyncMock, MagicMock


def _collect_sse(coro):
    """Run an async generator and return list of (event, data) tuples."""
    async def _gather():
        events = []
        async for chunk in coro:
            for block in chunk.strip().split("\n\n"):
                lines = block.strip().split("\n")
                event = None
                data = None
                for line in lines:
                    if line.startswith("event: "):
                        event = line[7:]
                    elif line.startswith("data: "):
                        data = json.loads(line[6:])
                events.append((event, data))
        return events
    return asyncio.new_event_loop().run_until_complete(_gather())
def _make_sse_db(dag_node_count=2):
    """Mock db + async_session for execute_all_nodes."""
    scalar_result = MagicMock()
    scalar_result.scalar.return_value = dag_node_count
    guard_result = MagicMock()
    guard_result.rowcount = 1
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[guard_result] + [scalar_result] * 20)
    db.commit = AsyncMock()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=db)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_async_session = MagicMock(return_value=mock_session_ctx)
    return db, mock_async_session


def _make_sse_db_guard_fails():
    """Mock where guard fails (job not found)."""
    guard_result = MagicMock()
    guard_result.rowcount = 0
    db = AsyncMock()
    db.execute = AsyncMock(return_value=guard_result)
    db.commit = AsyncMock()
    mock_session_ctx = AsyncMock()
    mock_session_ctx.__aenter__ = AsyncMock(return_value=db)
    mock_session_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_async_session = MagicMock(return_value=mock_session_ctx)
    return db, mock_async_session

    return db

@pytest.mark.smoke
class TestExecuteAllNodesSSESequence:
    """SSE events emitted in correct order for a 2-node happy path."""

    def test_happy_path_event_order(self):
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
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent.execute_next_node", mock_exec_next):
            from app.modules.execution_agent import execute_all_nodes
            events = _collect_sse(execute_all_nodes("job-1"))

        event_names = [e[0] for e in events]
        assert event_names == [
            "node_start", "node_done",
            "node_start", "node_done",
            "pipeline_complete",
        ]

    def test_pipeline_complete_has_summary_fields(self):
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
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent.execute_next_node", mock_exec_next):
            from app.modules.execution_agent import execute_all_nodes
            events = _collect_sse(execute_all_nodes("job-1"))

        complete_evt = [e for e in events if e[0] == "pipeline_complete"][0][1]
        assert "total_nodes" in complete_evt
        assert "passed" in complete_evt
        assert "failed" in complete_evt
        assert "duration_ms" in complete_evt
        assert complete_evt["passed"] == 1
        assert complete_evt["failed"] == 0

    def test_node_start_includes_tool(self):
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
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent.execute_next_node", mock_exec_next):
            from app.modules.execution_agent import execute_all_nodes
            events = _collect_sse(execute_all_nodes("job-1"))

        start_evt = events[0]
        assert start_evt[0] == "node_start"
        assert start_evt[1]["node_key"] == "T1"
        assert start_evt[1]["tool"] == "Milvus"


@pytest.mark.smoke
class TestExecuteAllNodesBlocked:
    """When T1 fails, downstream blocked nodes emit blocked SSE event."""

    def test_blocked_event_on_upstream_failure(self):
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
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent.execute_next_node", mock_exec_next):
            from app.modules.execution_agent import execute_all_nodes
            events = _collect_sse(execute_all_nodes("job-1"))

        event_names = [e[0] for e in events]
        assert "node_start" in event_names
        assert "node_failed" in event_names
        assert "blocked" in event_names

        blocked_evt = [e for e in events if e[0] == "blocked"][0][1]
        assert "blocked_nodes" in blocked_evt
        assert blocked_evt["blocked_nodes"][0]["node_key"] == "T2"
        assert blocked_evt["blocked_nodes"][0]["blocked_by"][0]["node_key"] == "T1"

    def test_invalid_job_returns_error_event(self):
        """Non-existent job emits a single error SSE event."""
        db, mock_session = _make_sse_db_guard_fails()
        mock_get_job = AsyncMock(return_value=None)
        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job", mock_get_job):
            from app.modules.execution_agent import execute_all_nodes
            events = _collect_sse(execute_all_nodes("bad-id"))

        assert len(events) == 1
        assert events[0][0] == "error"
        assert "not found" in events[0][1]["message"]


# ---------------------------------------------------------------------------
# Phase 4: _searxng_search() and _milvus_search() error handling
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestSearXNGSearchErrorHandling:
    """_searxng_search graceful degradation on failures.

    _searxng_search lazy-imports get_searxng_client inside the function,
    so the correct patch target is its source module, not execution_agent.
    """
    @staticmethod
    def _mock_client(*, response=None, side_effect=None):
        client = AsyncMock()
        if side_effect is not None:
            client.get = AsyncMock(side_effect=side_effect)
        else:
            client.get = AsyncMock(return_value=response)
        return client

    def test_http_error_returns_failure_string(self):
        from app.modules.execution_agent import _searxng_search
        resp = MagicMock()
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "503", request=MagicMock(), response=MagicMock(status_code=503)
        )
        client = self._mock_client(response=resp)
        with patch("app.utils.http_clients.get_searxng_client", return_value=client):
            result = _run(_searxng_search("test query"))
        assert "failed" in result.lower()

    def test_timeout_returns_failure_string(self):
        from app.modules.execution_agent import _searxng_search
        client = self._mock_client(side_effect=httpx.TimeoutException("timed out"))
        with patch("app.utils.http_clients.get_searxng_client", return_value=client):
            result = _run(_searxng_search("test query"))
        assert "failed" in result.lower()

    def test_empty_results_returns_no_results(self):
        from app.modules.execution_agent import _searxng_search
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": []}
        client = self._mock_client(response=resp)
        with patch("app.utils.http_clients.get_searxng_client", return_value=client):
            result = _run(_searxng_search("test query"))
        assert "no search results" in result.lower()

    def test_success_formats_results(self):
        from app.modules.execution_agent import _searxng_search
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {"results": [
            {"title": "Result 1", "content": "Snippet 1", "url": "https://example.com"},
        ]}
        client = self._mock_client(response=resp)
        with patch("app.utils.http_clients.get_searxng_client", return_value=client):
            result = _run(_searxng_search("test query"))
        assert "[1] Result 1" in result
        assert "Snippet 1" in result


@pytest.mark.smoke
class TestMilvusSearchErrorHandling:
    """_milvus_search graceful degradation on failures."""

    def test_connection_error_returns_failure_string(self):
        from app.modules.execution_agent import _milvus_search
        mock_query = AsyncMock(side_effect=ConnectionError("Milvus unreachable"))

        with patch("app.modules.execution_agent.query_rag", mock_query):
            result = _run(_milvus_search("test query"))
        assert "failed" in result.lower()

    def test_empty_results_returns_no_results(self):
        from app.modules.execution_agent import _milvus_search
        mock_query = AsyncMock(return_value={"results": []})

        with patch("app.modules.execution_agent.query_rag", mock_query):
            result = _run(_milvus_search("test query"))
        assert "no knowledge base results" in result.lower()

    def test_success_formats_results(self):
        from app.modules.execution_agent import _milvus_search
        mock_query = AsyncMock(return_value={"results": [
            {"title": "RAG Architecture", "content": "Retrieval-augmented generation..."},
            {"title": "Embeddings", "content": "Vector representations..."},
        ]})

        with patch("app.modules.execution_agent.query_rag", mock_query):
            result = _run(_milvus_search("test query"))
        assert "[1] RAG Architecture" in result
        assert "[2] Embeddings" in result
        assert "Retrieval-augmented" in result


# ---------------------------------------------------------------------------
# Phase B: abnormal-exit cleanup (#2), completed-status guard (#17),
# blocked-job compile cache (#22)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestExecuteAllNodesAbnormalExit:
    """#2: try/finally transitions stuck 'running' jobs to terminal state."""

    def test_exception_mid_loop_transitions_to_failed(self):
        """RuntimeError from execute_next_node → finally marks job 'failed' + emits execution_failed SSE."""
        guard_result = MagicMock(); guard_result.rowcount = 1
        dag_check = MagicMock(); dag_check.scalar.return_value = 1
        cleanup_status = MagicMock(); cleanup_status.scalar.return_value = "running"
        cleanup_update = MagicMock()

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            guard_result,     # Session 1 guard UPDATE
            dag_check,        # Session 3 DAG COUNT
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
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent.execute_next_node", mock_exec_next):
            from app.modules.execution_agent import execute_all_nodes
            events = _collect_sse(execute_all_nodes("job-1"))

        event_names = [e[0] for e in events]
        assert "execution_failed" in event_names, f"execution_failed missing from {event_names}"

        # Verify the cleanup UPDATE was called with 'failed'
        update_calls = [
            c for c in db.execute.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], dict)
               and c.args[1].get("s") == "failed"
        ]
        assert len(update_calls) >= 1, "Cleanup UPDATE with status='failed' not found"

    def test_cancelled_error_transitions_to_cancelled_and_reraises(self):
        """CancelledError from execute_next_node → finally marks 'cancelled' + re-raises."""
        guard_result = MagicMock(); guard_result.rowcount = 1
        dag_check = MagicMock(); dag_check.scalar.return_value = 1
        cleanup_status = MagicMock(); cleanup_status.scalar.return_value = "running"
        cleanup_update = MagicMock()

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            guard_result, dag_check, cleanup_status, cleanup_update,
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
                 patch("app.modules.execution_agent._get_next_node", mock_get_next), \
                 patch("app.modules.execution_agent.execute_next_node", mock_exec_next):
                from app.modules.execution_agent import execute_all_nodes
                try:
                    async for chunk in execute_all_nodes("job-1"):
                        events.append(chunk)
                except asyncio.CancelledError:
                    reraised = True
            return events, reraised

        _, reraised = asyncio.new_event_loop().run_until_complete(_run_collecting())
        assert reraised, "CancelledError was not re-raised after cleanup"

        update_calls = [
            c for c in db.execute.call_args_list
            if len(c.args) > 1 and isinstance(c.args[1], dict)
               and c.args[1].get("s") == "cancelled"
        ]
        assert len(update_calls) >= 1, "Cleanup UPDATE with status='cancelled' not found"

    def test_clean_exit_does_not_double_write_status(self):
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
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent.execute_next_node", mock_exec_next):
            from app.modules.execution_agent import execute_all_nodes
            events = _collect_sse(execute_all_nodes("job-1"))

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

    def test_completed_job_returns_409(self):
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
            events = _collect_sse(execute_all_nodes("job-1"))

        assert len(events) == 1
        assert events[0][0] == "error"
        assert events[0][1].get("http_status") == 409
        assert "completed" in events[0][1]["message"].lower()


@pytest.mark.smoke
class TestCompileOutputCache:
    """#22: blocked job with cached compiled_output skips recompute."""

    def test_cached_compiled_output_skips_recompute(self):
        """When jobs.compiled_output is populated, _compile_output is NOT called."""
        from app.modules.execution_agent import execute_next_node

        cached_result = MagicMock()
        cached_result.scalar.return_value = "CACHED COMPILED OUTPUT"
        status_update_result = MagicMock()
        blocked_query_result = MagicMock()
        blocked_query_result.fetchall.return_value = []

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            cached_result,         # SELECT compiled_output -> cache hit
            status_update_result,  # UPDATE status = 'blocked' (idempotent guard)
            blocked_query_result,  # SELECT for blocked_nodes detail
        ])
        db.commit = AsyncMock()

        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running", "refined_brief": {}
        })
        mock_get_next = AsyncMock(return_value=None)
        mock_all_done = AsyncMock(return_value=False)
        mock_compile = AsyncMock()  # MUST NOT be called

        with patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent._all_nodes_done", mock_all_done), \
             patch("app.modules.execution_agent._compile_output", mock_compile):
            result = _run(execute_next_node("job-1", db))

        assert result["status"] == "blocked"
        mock_compile.assert_not_called()

    def test_uncached_blocked_job_recomputes(self):
        """When compiled_output is NULL, _compile_output IS called and result stored."""
        from app.modules.execution_agent import execute_next_node

        cached_result = MagicMock()
        cached_result.scalar.return_value = None  # cache miss
        status_update_result = MagicMock()
        blocked_query_result = MagicMock()
        blocked_query_result.fetchall.return_value = []

        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[
            cached_result,         # SELECT compiled_output -> None
            status_update_result,  # UPDATE compiled_output + status
            blocked_query_result,  # SELECT for blocked_nodes detail
        ])
        db.commit = AsyncMock()

        mock_get_job = AsyncMock(return_value={
            "id": "job-1", "status": "running", "refined_brief": {}
        })
        mock_get_next = AsyncMock(return_value=None)
        mock_all_done = AsyncMock(return_value=False)
        mock_compile = AsyncMock(return_value="FRESHLY COMPILED")

        with patch("app.modules.execution_agent._get_job", mock_get_job), \
             patch("app.modules.execution_agent._get_next_node", mock_get_next), \
             patch("app.modules.execution_agent._all_nodes_done", mock_all_done), \
             patch("app.modules.execution_agent._compile_output", mock_compile):
            result = _run(execute_next_node("job-1", db))

        assert result["status"] == "blocked"
        mock_compile.assert_called_once()


# ---------------------------------------------------------------------------
# Phase C: is_output_node precedence (#97) + skip_node shape (#95)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestCompileOutputExplicitMarker:
    """#97: is_output_node=TRUE takes precedence over title heuristics."""

    def test_is_output_node_overrides_title_heuristic(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research topic", "tool": "SearXNG",
             "status": "done", "output_text": "should NOT be used",
             "is_output_node": False},
            {"node_key": "T2", "title": "Summarize output", "tool": "LLM",
             "status": "done", "output_text": "legacy heuristic winner",
             "is_output_node": False},
            {"node_key": "T3", "title": "Final deliverable", "tool": "LLM",
             "status": "done", "output_text": "EXPLICIT WINNER",
             "is_output_node": True},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert result == "EXPLICIT WINNER"

    def test_falls_back_to_heuristics_when_no_explicit_marker(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "LLM",
             "status": "done", "output_text": "research data",
             "is_output_node": False},
            {"node_key": "T2", "title": "Generate output", "tool": "LLM",
             "status": "done", "output_text": "heuristic-selected",
             "is_output_node": False},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert result == "heuristic-selected"

    def test_explicit_marker_but_not_done_falls_through(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "LLM",
             "status": "done", "output_text": "fallback content",
             "is_output_node": False},
            {"node_key": "T2", "title": "Compose output", "tool": "LLM",
             "status": "failed", "output_text": None,
             "is_output_node": True},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert "fallback content" in result
        assert "T2" not in result

    def test_multiple_output_nodes_joined(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Part A", "tool": "LLM",
             "status": "done", "output_text": "alpha",
             "is_output_node": True},
            {"node_key": "T2", "title": "Part B", "tool": "LLM",
             "status": "done", "output_text": "beta",
             "is_output_node": True},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert "alpha" in result
        assert "beta" in result
        assert "---" in result

    def test_backward_compat_no_marker_key(self):
        db = make_mock_db([
            {"node_key": "T1", "title": "Research", "tool": "LLM",
             "status": "done", "output_text": "legacy row"},
            {"node_key": "T2", "title": "Final output", "tool": "LLM",
             "status": "done", "output_text": "legacy heuristic pick"},
        ])
        from app.modules.execution_agent import _compile_output
        result = _run(_compile_output("job-1", db))
        assert result == "legacy heuristic pick"


@pytest.mark.smoke
class TestSkipNodeReturnShape:
    """#95: skip_node return dict conforms to ExecutionResult schema."""

    def test_skipped_return_conforms_to_schema(self):
        from app.modules.execution_agent import skip_node
        from app.schemas import ExecutionResult

        row_result = MagicMock()
        row_result.mappings.return_value.first.return_value = {"id": "node-uuid-1"}
        db = AsyncMock()
        db.execute = AsyncMock(return_value=row_result)
        db.commit = AsyncMock()

        result = _run(skip_node("job-1", "T1", db))

        validated = ExecutionResult(**result)
        assert validated.status == "skipped"
        assert validated.node_key == "T1"

    def test_not_found_return_conforms_to_schema(self):
        from app.modules.execution_agent import skip_node
        from app.schemas import ExecutionResult

        row_result = MagicMock()
        row_result.mappings.return_value.first.return_value = None
        db = AsyncMock()
        db.execute = AsyncMock(return_value=row_result)

        result = _run(skip_node("job-1", "T99", db))

        validated = ExecutionResult(**result)
        assert validated.status == "error"
        assert validated.message is not None
        assert "not found" in validated.message.lower()
