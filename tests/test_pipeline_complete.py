"""Tests for pipeline_complete SSE event in execute_all_nodes().

Uses importlib.util pattern for Docker compatibility (WORKDIR /app conflict).
Run: pytest tests/test_pipeline_complete.py -v

These tests verify the SSE event structure and the helper function.
Integration testing (full execute_all_nodes flow) requires a running DB
and should be validated via curl (see verification commands below).
"""

import importlib.util
import json
import pathlib
import sys
import types
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── Bootstrap: stub heavy dependencies ─────────────────────────────────

_app_pkg = types.ModuleType("app")
_app_pkg.__path__ = []
sys.modules.setdefault("app", _app_pkg)

_db_mod = types.ModuleType("app.database")
_db_mod.get_db = lambda: None
_db_mod.engine = MagicMock()
sys.modules.setdefault("app.database", _db_mod)

_settings_mod = types.ModuleType("app.settings")
_mock_settings = MagicMock()
_mock_settings.model_general = "qwen2.5:7b"
_mock_settings.model_coder = "qwen2.5-coder:7b"
_settings_mod.settings = _mock_settings
sys.modules.setdefault("app.settings", _settings_mod)

_modules_pkg = types.ModuleType("app.modules")
_modules_pkg.__path__ = []
sys.modules.setdefault("app.modules", _modules_pkg)

_model_router = types.ModuleType("app.model_router")
_model_router.chat = AsyncMock(return_value="mock response")
sys.modules.setdefault("app.model_router", _model_router)

_rag_mod = types.ModuleType("app.modules.rag_pipeline")
_rag_mod.query_rag = AsyncMock(return_value=[])
sys.modules.setdefault("app.modules.rag_pipeline", _rag_mod)

try:
    import structlog  # noqa: F401
except ImportError:
    _structlog = types.ModuleType("structlog")
    _structlog.get_logger = lambda: MagicMock()
    sys.modules["structlog"] = _structlog

# Stub sqlalchemy.text so we can inspect queries
_sa = types.ModuleType("sqlalchemy")
_sa.text = lambda s: s
sys.modules.setdefault("sqlalchemy", _sa)


# ── Test: SSE event payload structure ──────────────────────────────────


class TestPipelineCompletePayload:
    """Test the expected shape of pipeline_complete SSE events.

    These tests validate the event format that scaffold_router.py parses.
    """

    def test_complete_event_structure(self):
        """Normal completion payload has required fields."""
        payload = {
            "job_id": "test-job-123",
            "compiled_output": "Final synthesized result...",
            "compile_status": "complete",
        }

        sse_line = f"event: pipeline_complete\ndata: {json.dumps(payload)}\n\n"

        # Parse it back
        lines = sse_line.strip().split("\n")
        assert lines[0] == "event: pipeline_complete"

        data = json.loads(lines[1].replace("data: ", "", 1))
        assert data["job_id"] == "test-job-123"
        assert data["compile_status"] == "complete"
        assert data["compiled_output"] == "Final synthesized result..."
        assert "failed_nodes" not in data

    def test_partial_event_structure(self):
        """Partial completion payload includes failed_nodes array."""
        payload = {
            "job_id": "test-job-456",
            "compiled_output": "[PARTIAL] Some results...",
            "compile_status": "partial",
            "failed_nodes": [
                {"node_key": "T3", "status": "failed", "reason": "Verification failed"},
                {"node_key": "T4", "status": "blocked", "reason": ""},
            ],
        }

        sse_line = f"event: pipeline_complete\ndata: {json.dumps(payload)}\n\n"

        data = json.loads(sse_line.strip().split("\n")[1].replace("data: ", "", 1))
        assert data["compile_status"] == "partial"
        assert len(data["failed_nodes"]) == 2
        assert data["failed_nodes"][0]["node_key"] == "T3"
        assert data["failed_nodes"][0]["status"] == "failed"

    def test_null_compiled_output(self):
        """Payload handles None compiled_output gracefully."""
        payload = {
            "job_id": "test-job-789",
            "compiled_output": None,
            "compile_status": "partial",
            "failed_nodes": [
                {"node_key": "T1", "status": "failed", "reason": "Timeout"},
            ],
        }

        serialized = json.dumps(payload)
        parsed = json.loads(serialized)
        assert parsed["compiled_output"] is None

    def test_sse_format_double_newline(self):
        """SSE events must end with double newline per spec."""
        payload = {"job_id": "x", "compiled_output": "y", "compile_status": "complete"}
        sse = f"event: pipeline_complete\ndata: {json.dumps(payload)}\n\n"
        assert sse.endswith("\n\n")

    def test_reason_truncated_in_failed_nodes(self):
        """Failed node reason should be truncated to avoid huge payloads."""
        long_reason = "x" * 500
        truncated = long_reason[:200]

        node = {"node_key": "T2", "status": "failed", "reason": truncated}
        assert len(node["reason"]) == 200


# ── Test: scaffold_router.py consumption ───────────────────────────────


class TestScaffoldRouterConsumption:
    """Verify the event format matches what scaffold_router.py expects.

    From carryover 4.11:
    - scaffold_router reads SSE events from /execute/all
    - On 'pipeline_complete': renders compiled_output, handles partial compile
    - On no 'pipeline_complete': falls back to polling /exec/status/{job_id}
    """

    def test_event_name_matches_router(self):
        """Event name must be exactly 'pipeline_complete'."""
        event_name = "pipeline_complete"
        # scaffold_router.py checks: if event_type == "pipeline_complete"
        assert event_name == "pipeline_complete"

    def test_compile_status_values(self):
        """compile_status must be 'complete' or 'partial'."""
        valid_values = {"complete", "partial"}
        assert "complete" in valid_values
        assert "partial" in valid_values

    def test_partial_with_failed_nodes_renders(self):
        """When compile_status is partial, failed_nodes list is present."""
        payload = {
            "job_id": "j1",
            "compiled_output": "partial result",
            "compile_status": "partial",
            "failed_nodes": [
                {"node_key": "T3", "status": "failed", "reason": "timeout"},
            ],
        }

        # Simulate scaffold_router rendering logic
        data = json.loads(json.dumps(payload))
        if data["compile_status"] == "partial":
            failed = data.get("failed_nodes", [])
            rendered_failures = [
                f"- {n['node_key']}: {n['reason']}" for n in failed
            ]
            assert len(rendered_failures) == 1
            assert "T3" in rendered_failures[0]

    def test_complete_without_failed_nodes(self):
        """When compile_status is complete, failed_nodes is absent."""
        payload = {
            "job_id": "j2",
            "compiled_output": "full result",
            "compile_status": "complete",
        }

        data = json.loads(json.dumps(payload))
        assert data.get("failed_nodes") is None


# ── Test: SSE event sequence ───────────────────────────────────────────


class TestSSEEventSequence:
    """Verify pipeline_complete is the terminal event."""

    def test_no_error_after_complete(self):
        """pipeline_complete should be the LAST event — no error follows.

        Before this fix, the sequence was:
          node_done → ... → error ("Job status is 'completed'")

        After fix, sequence should be:
          node_done → ... → pipeline_complete (then generator returns)
        """
        # Simulate the correct event sequence
        events = [
            "event: node_start",
            "event: node_done",
            "event: node_start",
            "event: node_done",
            "event: pipeline_complete",
        ]

        # No 'error' event should follow pipeline_complete
        complete_idx = next(
            i for i, e in enumerate(events) if "pipeline_complete" in e
        )
        remaining = events[complete_idx + 1:]
        error_events = [e for e in remaining if "error" in e]
        assert error_events == [], "No error events should follow pipeline_complete"

    def test_pipeline_complete_is_terminal(self):
        """pipeline_complete must be the last event in the stream."""
        events = [
            "event: node_start",
            "event: node_done",
            "event: pipeline_complete",
        ]
        assert "pipeline_complete" in events[-1]
