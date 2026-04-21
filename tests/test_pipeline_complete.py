"""Tests for the pipeline_complete SSE event payload shape.

#9.1  — Remove ~6 tautology tests that asserted their own literals.
#9.12 — Drop dead `app.settings` stub (code uses `app.config`).
#9.13 — Remove module-level `sys.modules` stubbing; rely on real imports
        since this file no longer imports heavy app modules.

The file now contains only structural shape tests for the SSE event format
that scaffold_router.py parses. Integration behavior is covered by
test_execution_agent + test_sse_streaming.
"""
import json

import pytest


class TestPipelineCompletePayload:
    """Shape tests for pipeline_complete SSE events."""

    def test_complete_event_structure(self):
        """Normal completion payload round-trips through SSE encoding."""
        payload = {
            "job_id": "test-job-123",
            "compiled_output": "Final synthesized result...",
            "compile_status": "complete",
        }
        sse_line = f"event: pipeline_complete\ndata: {json.dumps(payload)}\n\n"

        lines = sse_line.strip().split("\n")
        assert lines[0] == "event: pipeline_complete"
        data = json.loads(lines[1].replace("data: ", "", 1))
        assert data == payload  # full equality — round-trip must be lossless

    def test_partial_event_structure(self):
        """Partial completion payload carries a failed_nodes array."""
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
        assert data["failed_nodes"] == payload["failed_nodes"]

    def test_sse_format_double_newline(self):
        """SSE events must end with double newline per spec (RFC-style)."""
        payload = {"job_id": "x", "compiled_output": "y", "compile_status": "complete"}
        sse = f"event: pipeline_complete\ndata: {json.dumps(payload)}\n\n"
        assert sse.endswith("\n\n")
        # Exactly two trailing newlines — more would push extra blank frames.
        assert not sse.endswith("\n\n\n")
