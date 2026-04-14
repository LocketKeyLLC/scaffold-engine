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
    """_searxng_search graceful degradation on failures."""

    def test_http_error_returns_failure_string(self):
        from app.modules.execution_agent import _searxng_search
        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "503", request=MagicMock(), response=MagicMock(status_code=503)
        )
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("app.modules.execution_agent.httpx.AsyncClient", return_value=mock_client):
            result = _run(_searxng_search("test query"))
        assert "failed" in result.lower()

    def test_timeout_returns_failure_string(self):
        from app.modules.execution_agent import _searxng_search
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

        with patch("app.modules.execution_agent.httpx.AsyncClient", return_value=mock_client):
            result = _run(_searxng_search("test query"))
        assert "failed" in result.lower()

    def test_empty_results_returns_no_results(self):
        from app.modules.execution_agent import _searxng_search
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": []}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("app.modules.execution_agent.httpx.AsyncClient", return_value=mock_client):
            result = _run(_searxng_search("test query"))
        assert "no search results" in result.lower()

    def test_success_formats_results(self):
        from app.modules.execution_agent import _searxng_search
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"results": [
            {"title": "Result 1", "content": "Snippet 1", "url": "https://example.com"},
        ]}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("app.modules.execution_agent.httpx.AsyncClient", return_value=mock_client):
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
