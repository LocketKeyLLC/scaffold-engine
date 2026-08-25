"""
tests/test_sse_streaming.py - SSE streaming behavioral tests

Run:  docker exec scaffold-orchestrator pytest tests/test_sse_streaming.py -m smoke --timeout=30 -v
"""

import json
import asyncio
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch


@pytest.fixture(autouse=True)
def _force_sequential_execution(monkeypatch):
    """§17.827 (plan 7.3) — pin sequential execution mode for this module.

    The SSE-behavior assertions here (event order, pipeline_complete shape,
    keepalive watchdog) encode the sequential execute_all_nodes contract;
    under the code default ``parallel_execution_enabled=True`` 11 of the 18
    tests fail. See the twin fixture in test_execution_agent_sse.py for the
    full history (§17.589 misattribution).
    """
    from app.config import settings
    monkeypatch.setattr(settings, "parallel_execution_enabled", False)


# ---------------------------------------------------------------------------
# Helpers (same pattern as test_execution_agent.py)
# ---------------------------------------------------------------------------

def _collect_sse(async_gen):
    """Run an async generator and return list of (event_name, data_dict) tuples."""
    async def _gather():
        events = []
        async for chunk in async_gen:
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


def _collect_sse_raw(async_gen):
    """Run an async generator and return raw SSE strings."""
    async def _gather():
        chunks = []
        async for chunk in async_gen:
            chunks.append(chunk)
        return chunks
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
    """Mock where concurrent guard fails (job already running or not found)."""
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


def _node(key, title, tool):
    return {"node_key": key, "title": title, "tool": tool}


def _done(key, title):
    return {
        "status": "done", "node_key": key, "title": title,
        "output": "ok", "verified": True, "confidence": 0.9,
        "model_used": "m",
    }


def _failed(key, title):
    return {
        "status": "failed", "node_key": key, "title": title,
        "error": "verification failed",
    }


_COMPLETE = {"status": "complete"}


# ===========================================================================
# SSE Wire Format
# ===========================================================================

@pytest.mark.smoke
class TestSSEWireFormat:
    """Verify SSE events conform to the text/event-stream spec."""

    def test_each_chunk_has_event_and_data_lines(self):
        """Every yielded chunk contains 'event: ...' and 'data: ...' lines."""
        db, mock_session = _make_sse_db(dag_node_count=1)
        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job",
                   AsyncMock(return_value={"status": "executing", "id": "j1"})), \
             patch("app.modules.execution_agent._peek_next_node",
                   AsyncMock(side_effect=[_node("T1", "X", "LLM"), None])), \
             patch("app.modules.execution_agent.execute_next_node",
                   AsyncMock(side_effect=[_done("T1", "X"), _COMPLETE])):
            from app.modules.execution_agent import execute_all_nodes
            raw = _collect_sse_raw(execute_all_nodes("j1"))
        for chunk in raw:
            assert "event: " in chunk, "Missing event: line in chunk"
            assert "data: " in chunk, "Missing data: line in chunk"

    def test_data_lines_are_valid_json(self):
        """Every data: line parses as valid JSON."""
        db, mock_session = _make_sse_db(dag_node_count=1)
        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job",
                   AsyncMock(return_value={"status": "executing", "id": "j1"})), \
             patch("app.modules.execution_agent._peek_next_node",
                   AsyncMock(side_effect=[_node("T1", "X", "LLM"), None])), \
             patch("app.modules.execution_agent.execute_next_node",
                   AsyncMock(side_effect=[_done("T1", "X"), _COMPLETE])):
            from app.modules.execution_agent import execute_all_nodes
            raw = _collect_sse_raw(execute_all_nodes("j1"))
        for chunk in raw:
            for line in chunk.strip().split("\n"):
                if line.startswith("data: "):
                    parsed = json.loads(line[6:])
                    assert isinstance(parsed, dict)

    def test_chunks_end_with_double_newline(self):
        """SSE spec requires each event block ends with \\n\\n."""
        db, mock_session = _make_sse_db(dag_node_count=1)
        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job",
                   AsyncMock(return_value={"status": "executing", "id": "j1"})), \
             patch("app.modules.execution_agent._peek_next_node",
                   AsyncMock(side_effect=[_node("T1", "X", "LLM"), None])), \
             patch("app.modules.execution_agent.execute_next_node",
                   AsyncMock(side_effect=[_done("T1", "X"), _COMPLETE])):
            from app.modules.execution_agent import execute_all_nodes
            raw = _collect_sse_raw(execute_all_nodes("j1"))
        for chunk in raw:
            assert chunk.endswith("\n\n"), "SSE chunk must end with double newline"


# ===========================================================================
# Event Sequence Contract
# ===========================================================================

@pytest.mark.smoke
class TestEventSequenceContract:
    """Verify SSE events arrive in the correct order."""

    def test_happy_path_2_nodes(self):
        """node_start -> node_done -> node_start -> node_done -> pipeline_complete"""
        db, mock_session = _make_sse_db(dag_node_count=2)
        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job",
                   AsyncMock(return_value={"status": "executing", "id": "j1"})), \
             patch("app.modules.execution_agent._peek_next_node",
                   AsyncMock(side_effect=[
                       _node("T1", "A", "LLM"),
                       _node("T2", "B", "SearXNG"),
                       None])), \
             patch("app.modules.execution_agent.execute_next_node",
                   AsyncMock(side_effect=[
                       _done("T1", "A"), _done("T2", "B"), _COMPLETE])):
            from app.modules.execution_agent import execute_all_nodes
            events = _collect_sse(execute_all_nodes("j1"))
        names = [e[0] for e in events]
        assert names == [
            "node_start", "node_done",
            "node_start", "node_done",
            "pipeline_complete",
        ]

    def test_failed_node_emits_node_failed(self):
        """A node that fails verification yields node_failed, not node_done."""
        db, mock_session = _make_sse_db(dag_node_count=1)
        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job",
                   AsyncMock(return_value={"status": "executing", "id": "j1"})), \
             patch("app.modules.execution_agent._peek_next_node",
                   AsyncMock(side_effect=[_node("T1", "X", "LLM"), None])), \
             patch("app.modules.execution_agent.execute_next_node",
                   AsyncMock(side_effect=[_failed("T1", "X"), _COMPLETE])):
            from app.modules.execution_agent import execute_all_nodes
            events = _collect_sse(execute_all_nodes("j1"))
        names = [e[0] for e in events]
        assert "node_start" in names
        assert "node_failed" in names
        assert "pipeline_complete" in names
        assert "node_done" not in names

    def test_pipeline_complete_is_always_last(self):
        """pipeline_complete is the final event in every run."""
        db, mock_session = _make_sse_db(dag_node_count=1)
        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job",
                   AsyncMock(return_value={"status": "executing", "id": "j1"})), \
             patch("app.modules.execution_agent._peek_next_node",
                   AsyncMock(side_effect=[_node("T1", "X", "LLM"), None])), \
             patch("app.modules.execution_agent.execute_next_node",
                   AsyncMock(side_effect=[_done("T1", "X"), _COMPLETE])):
            from app.modules.execution_agent import execute_all_nodes
            events = _collect_sse(execute_all_nodes("j1"))
        assert events[-1][0] == "pipeline_complete"


# ===========================================================================
# pipeline_complete Event Structure
# ===========================================================================

@pytest.mark.smoke
class TestPipelineCompleteStructure:
    """Verify pipeline_complete payload contains required fields."""

    def _run_single_node(self, verified=True):
        db, mock_session = _make_sse_db(dag_node_count=1)
        result = _done("T1", "X") if verified else _failed("T1", "X")
        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job",
                   AsyncMock(return_value={"status": "executing", "id": "j1"})), \
             patch("app.modules.execution_agent._peek_next_node",
                   AsyncMock(side_effect=[_node("T1", "X", "LLM"), None])), \
             patch("app.modules.execution_agent.execute_next_node",
                   AsyncMock(side_effect=[result, _COMPLETE])):
            from app.modules.execution_agent import execute_all_nodes
            events = _collect_sse(execute_all_nodes("j1"))
        return [e for e in events if e[0] == "pipeline_complete"][0][1]

    def test_has_total_nodes(self):
        data = self._run_single_node()
        assert "total_nodes" in data
        assert data["total_nodes"] == 1

    def test_has_passed_and_failed_counts(self):
        data = self._run_single_node()
        assert data["passed"] == 1
        assert data["failed"] == 0

    def test_has_duration_ms(self):
        data = self._run_single_node()
        assert "duration_ms" in data
        assert isinstance(data["duration_ms"], (int, float))

    def test_has_compile_status(self):
        data = self._run_single_node()
        assert data["compile_status"] in ("complete", "partial")

    def test_partial_includes_failed_nodes(self):
        data = self._run_single_node(verified=False)
        assert data["compile_status"] == "partial"
        assert "failed_nodes" in data
        assert len(data["failed_nodes"]) == 1
        assert data["failed_nodes"][0]["node_key"] == "T1"

    def test_complete_has_no_failed_nodes_key(self):
        data = self._run_single_node(verified=True)
        assert data["compile_status"] == "complete"
        assert "failed_nodes" not in data


# ===========================================================================
# Concurrent Execution Guard
# ===========================================================================

@pytest.mark.smoke
class TestConcurrentGuard:
    """Verify the atomic check-and-set guard prevents duplicate runs."""

    def test_guard_failure_yields_error(self):
        """When guard UPDATE matches 0 rows, an error event is yielded."""
        _, mock_session = _make_sse_db_guard_fails()
        with patch("app.modules.execution_agent.async_session", mock_session):
            from app.modules.execution_agent import execute_all_nodes
            events = _collect_sse(execute_all_nodes("job-1"))
        names = [e[0] for e in events]
        assert "error" in names
        assert "pipeline_complete" not in names

    def test_guard_error_message_references_job(self):
        """Guard failure error message includes the job ID."""
        _, mock_session = _make_sse_db_guard_fails()
        with patch("app.modules.execution_agent.async_session", mock_session):
            from app.modules.execution_agent import execute_all_nodes
            events = _collect_sse(execute_all_nodes("job-99"))
        error_events = [e[1] for e in events if e[0] == "error"]
        assert len(error_events) >= 1
        assert "job-99" in error_events[0].get("message", "")


# ===========================================================================
# node_start Event Fields
# ===========================================================================

@pytest.mark.smoke
class TestNodeStartEvent:
    """Verify node_start events contain required fields."""

    def test_node_start_has_key_title_tool(self):
        db, mock_session = _make_sse_db(dag_node_count=1)
        with patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job",
                   AsyncMock(return_value={"status": "executing", "id": "j1"})), \
             patch("app.modules.execution_agent._peek_next_node",
                   AsyncMock(side_effect=[
                       _node("T1", "Research", "Milvus"), None])), \
             patch("app.modules.execution_agent.execute_next_node",
                   AsyncMock(side_effect=[_done("T1", "Research"), _COMPLETE])):
            from app.modules.execution_agent import execute_all_nodes
            events = _collect_sse(execute_all_nodes("j1"))
        start_evt = [e for e in events if e[0] == "node_start"][0][1]
        assert start_evt["node_key"] == "T1"
        assert start_evt["title"] == "Research"
        assert start_evt["tool"] == "Milvus"


# ===========================================================================
# Heartbeat Character
# ===========================================================================

@pytest.mark.smoke
class TestHeartbeatCharacter:
    """Verify the SSE keepalive uses zero-width space."""

    def test_keepalive_is_zero_width_space(self):
        """scaffold_router.py uses the zero-width space as keepalive."""
        router_path = os.path.abspath(os.path.join(
            os.path.dirname(__file__), "..", "pipelines", "scaffold_router.py"
        ))
        if not os.path.exists(router_path):
            pytest.skip("scaffold_router.py not in container")
        with open(router_path, "r") as f:
            source = f.read()
        # Zero-width space U+200B should be present as the keepalive char
        assert "\u200b" in source or "\\u200b" in source, (
            "Keepalive should use zero-width space"
        )


# ===========================================================================
# \u00a717.261 \u2014 _keepalive_loop watchdog wired as progress logger
# ===========================================================================

@pytest.mark.smoke
class TestKeepaliveProgressWatchdog:
    """Section 17.261 - wired-up _keepalive_loop logs exec_node_still_running
    per keepalive tick while exec_task runs. Pure observability - no
    SSE event added, no lifecycle change. Closes section 17.258 red #3."""

    def test_long_running_exec_emits_progress_log(self, caplog):
        """A node that takes longer than sse_keepalive_seconds must produce
        at least one `exec_node_still_running` log line carrying job_id +
        node_key + elapsed_s."""
        from app.config import settings
        import logging

        # Slow exec_task: first call awaits ~0.18s and returns _done(),
        # second call returns _COMPLETE. With sse_keepalive_seconds=0.05,
        # the watchdog ticks ~3 times during the first call.
        call_count = {"n": 0}

        async def _exec_side(*_a, **_kw):
            call_count["n"] += 1
            if call_count["n"] == 1:
                await asyncio.sleep(0.18)
                return _done("T1", "X")
            return _COMPLETE

        db, mock_session = _make_sse_db(dag_node_count=1)
        with caplog.at_level(logging.INFO, logger="app.modules.execution_agent"), \
             patch.object(settings, "sse_keepalive_seconds", 0.05), \
             patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job",
                   AsyncMock(return_value={"status": "executing", "id": "job-261"})), \
             patch("app.modules.execution_agent._peek_next_node",
                   AsyncMock(side_effect=[_node("T1", "X", "LLM"), None])), \
             patch("app.modules.execution_agent.execute_next_node", _exec_side):
            from app.modules.execution_agent import execute_all_nodes
            _collect_sse_raw(execute_all_nodes("job-261"))

        progress_lines = [
            r for r in caplog.records
            if "exec_node_still_running" in r.getMessage()
        ]
        assert progress_lines, (
            f"expected \u22651 'exec_node_still_running' log line from \u00a717.261 "
            f"watchdog; got: {[r.getMessage() for r in caplog.records]}"
        )
        # The log line carries job_id and node_key for grep-debugging hangs.
        msg = progress_lines[0].getMessage()
        assert "job=job-261" in msg, f"missing job_id in log: {msg!r}"
        assert "node=T1" in msg, f"missing node_key in log: {msg!r}"

    def test_fast_exec_emits_no_progress_log(self, caplog):
        """A node that finishes within one keepalive tick must NOT produce
        any `exec_node_still_running` lines \u2014 the watchdog only fires after
        the timeout elapses without keepalive_stop being set."""
        from app.config import settings
        import logging

        db, mock_session = _make_sse_db(dag_node_count=1)
        # Default sse_keepalive_seconds=15.0 vs instant exec \u2014 no tick fires.
        with caplog.at_level(logging.INFO, logger="app.modules.execution_agent"), \
             patch("app.modules.execution_agent.async_session", mock_session), \
             patch("app.modules.execution_agent._get_job",
                   AsyncMock(return_value={"status": "executing", "id": "j1"})), \
             patch("app.modules.execution_agent._peek_next_node",
                   AsyncMock(side_effect=[_node("T1", "X", "LLM"), None])), \
             patch("app.modules.execution_agent.execute_next_node",
                   AsyncMock(side_effect=[_done("T1", "X"), _COMPLETE])):
            from app.modules.execution_agent import execute_all_nodes
            _collect_sse_raw(execute_all_nodes("j1"))

        progress_lines = [
            r for r in caplog.records
            if "exec_node_still_running" in r.getMessage()
        ]
        assert not progress_lines, (
            f"watchdog should not fire on fast exec; got: "
            f"{[r.getMessage() for r in progress_lines]}"
        )
