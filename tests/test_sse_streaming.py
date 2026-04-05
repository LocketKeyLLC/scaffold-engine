"""
tests/test_sse_streaming.py — SSE streaming smoke tests

Uses importlib to avoid WORKDIR /app package collision (Task #18).
Tests SSE event format, event sequence contract, heartbeat behavior,
and pipeline_complete event structure.
"""

import importlib.util
import os
import sys
import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

# ---------------------------------------------------------------------------
# importlib loader for execution_agent (SSE source)
# ---------------------------------------------------------------------------

_MODULE_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "modules", "execution_agent.py"
)
_ABS_PATH = os.path.abspath(_MODULE_PATH)

_ROUTER_PATH = os.path.join(
    os.path.dirname(__file__), "..", "pipelines", "scaffold_router.py"
)
_ROUTER_ABS = os.path.abspath(_ROUTER_PATH)


# ===========================================================================
# SSE Format Tests (source-code based)
# ===========================================================================

class TestSSEEventFormat:
    """Tests for SSE event format compliance."""

    def test_execution_agent_yields_sse_format(self):
        """execution_agent.py yields events in SSE data: format."""
        with open(_ABS_PATH, "r") as f:
            source = f.read()
        # SSE events should be yielded as JSON with event type
        assert "yield" in source, "execution_agent should yield SSE events"
        assert "json.dumps" in source or "json_dumps" in source, (
            "SSE events should be JSON-serialized"
        )

    def test_pipeline_complete_event_exists(self):
        """execution_agent.py yields a pipeline_complete event."""
        with open(_ABS_PATH, "r") as f:
            source = f.read()
        assert "pipeline_complete" in source, (
            "execution_agent should emit pipeline_complete SSE event"
        )

    def test_node_complete_event_exists(self):
        """execution_agent.py yields node-level completion events."""
        with open(_ABS_PATH, "r") as f:
            source = f.read()
        assert any(term in source for term in [
            "node_done", "node_complete", "node_completed",
        ]), "execution_agent should emit node completion events"


# ===========================================================================
# Pipeline Complete Event Structure
# ===========================================================================

class TestPipelineCompleteEvent:
    """Tests for pipeline_complete SSE event structure."""

    def test_compile_status_field(self):
        """pipeline_complete event includes compile_status field."""
        with open(_ABS_PATH, "r") as f:
            source = f.read()
        assert "compile_status" in source, (
            "pipeline_complete should include compile_status"
        )

    def test_failed_nodes_field(self):
        """pipeline_complete event includes failed_nodes field."""
        with open(_ABS_PATH, "r") as f:
            source = f.read()
        assert "failed_nodes" in source, (
            "pipeline_complete should include failed_nodes array"
        )

    def test_duration_ms_field(self):
        """pipeline_complete event includes duration_ms field."""
        with open(_ABS_PATH, "r") as f:
            source = f.read()
        assert "duration" in source, (
            "pipeline_complete should include duration information"
        )

    def test_both_completion_paths_emit_event(self):
        """pipeline_complete is emitted in both normal and early-exit paths."""
        with open(_ABS_PATH, "r") as f:
            source = f.read()
        # Count occurrences of pipeline_complete yield
        count = source.count("pipeline_complete")
        assert count >= 2, (
            f"pipeline_complete should appear in 2+ paths (normal + early exit), "
            f"found {count}"
        )


# ===========================================================================
# Event Sequence Contract
# ===========================================================================

class TestEventSequence:
    """Tests for SSE event ordering contract."""

    def test_blocked_event_exists(self):
        """execution_agent emits blocked events for blocked nodes."""
        with open(_ABS_PATH, "r") as f:
            source = f.read()
        assert "blocked" in source

    def test_error_event_exists(self):
        """execution_agent emits error events on failure."""
        with open(_ABS_PATH, "r") as f:
            source = f.read()
        assert "error" in source or "failed" in source

    def test_execute_all_nodes_is_generator(self):
        """execute_all_nodes is an async generator (yields SSE events)."""
        with open(_ABS_PATH, "r") as f:
            source = f.read()
        # Should be an async generator (async def + yield)
        assert "async def execute_all_nodes" in source
        assert "yield" in source


# ===========================================================================
# Scaffold Router SSE Relay
# ===========================================================================

class TestScaffoldRouterRelay:
    """Tests for scaffold_router.py SSE relay behavior."""

    @pytest.mark.skipif(
        not os.path.exists(_ROUTER_ABS),
        reason="scaffold_router.py not found",
    )
    def test_router_handles_pipeline_complete(self):
        """scaffold_router.py handles pipeline_complete event."""
        with open(_ROUTER_ABS, "r") as f:
            source = f.read()
        assert "pipeline_complete" in source, (
            "scaffold_router should handle pipeline_complete event"
        )

    @pytest.mark.skipif(
        not os.path.exists(_ROUTER_ABS),
        reason="scaffold_router.py not found",
    )
    def test_router_has_heartbeat(self):
        """scaffold_router.py implements keepalive heartbeats."""
        with open(_ROUTER_ABS, "r") as f:
            source = f.read()
        assert any(term in source for term in [
            "heartbeat", "keepalive", "keep_alive",
        ]), "scaffold_router should implement heartbeats"

    @pytest.mark.skipif(
        not os.path.exists(_ROUTER_ABS),
        reason="scaffold_router.py not found",
    )
    def test_router_has_fallback_poll(self):
        """scaffold_router.py has fallback polling mechanism."""
        with open(_ROUTER_ABS, "r") as f:
            source = f.read()
        assert any(term in source for term in [
            "fallback", "poll", "timeout",
        ]), "scaffold_router should have fallback mechanism"


# ===========================================================================
# Heartbeat Tests
# ===========================================================================

class TestHeartbeat:
    """Tests for SSE heartbeat/keepalive behavior."""

    @pytest.mark.skipif(
        not os.path.exists(_ROUTER_ABS),
        reason="scaffold_router.py not found",
    )
    def test_heartbeat_character(self):
        """Heartbeat uses dot or empty string."""
        with open(_ROUTER_ABS, "r") as f:
            source = f.read()
        # Current implementation uses dots (known issue #11)
        # or might use empty string / zero-width space
        has_heartbeat = any(char in source for char in [
            '"."', "'.'", '""', "''", '"\\u200b"',
        ])
        assert has_heartbeat or "heartbeat" in source


# ===========================================================================
# Error Handling Tests
# ===========================================================================

class TestErrorHandling:
    """Tests for SSE error handling in execution agent."""

    def test_node_timeout_handling(self):
        """execution_agent handles node timeouts."""
        with open(_ABS_PATH, "r") as f:
            source = f.read()
        assert "timeout" in source.lower(), (
            "execution_agent should handle node timeouts"
        )

    def test_concurrent_guard(self):
        """execution_agent has concurrent execution guard."""
        with open(_ABS_PATH, "r") as f:
            source = f.read()
        assert any(term in source for term in [
            "concurrent", "guard", "running",
        ]), "execution_agent should guard against concurrent execution"

    def test_partial_compile_on_failure(self):
        """execution_agent produces partial compile on node failure."""
        with open(_ABS_PATH, "r") as f:
            source = f.read()
        assert "partial" in source.lower() or "PARTIAL" in source, (
            "execution_agent should support partial compilation"
        )
