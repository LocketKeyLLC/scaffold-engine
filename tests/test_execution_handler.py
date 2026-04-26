"""
Tests for pipelines/execution_handler.py

Covers fixes:
  #8.4  — _approve reads model_used (not 'model')
  #8.5  — _approve reads output (not 'output_preview') with truncation
  #8.16 — _status uses .get() guards, no crash on partial response
  #8.24 — resp.json() raising renders clear error, not traceback

Plus regression guards for _skip/_retry response-shape handling.
"""

import json
import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import requests

_HANDLER_PATH = Path(__file__).resolve().parents[1] / "pipelines" / "execution_handler.py"
if not _HANDLER_PATH.exists():
    pytest.skip(
        f"execution_handler.py not found at {_HANDLER_PATH} — skipping (expected in pipelines/ directory)",
        allow_module_level=True,
    )

_SPEC = importlib.util.spec_from_file_location("execution_handler", _HANDLER_PATH)
execution_handler = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(execution_handler)
Pipeline = execution_handler.Pipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_resp(status_code=200, json_data=None, text="", raise_on_json=False):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if raise_on_json:
        resp.json.side_effect = json.JSONDecodeError("bad", "doc", 0)
    else:
        resp.json.return_value = json_data if json_data is not None else {}
    return resp


def _pipeline():
    p = Pipeline()
    p.valves.api_key = "test-key"
    p.valves.orchestrator_url = "http://test:8000"
    return p


# ---------------------------------------------------------------------------
# #8.4 / #8.5 — _approve field names
# ---------------------------------------------------------------------------

class TestApproveFieldNames:
    """#8.4: model_used is surfaced. #8.5: output renders (not output_preview)."""

    def test_model_used_rendered(self):
        resp = _mock_resp(json_data={
            "status": "done",
            "node_key": "T1",
            "title": "Research topic",
            "output": "some short output",
            "verified": True,
            "confidence": 0.87,
            "model_used": "qwen3:4b",
            "awaiting_approval": True,
        })
        p = _pipeline()
        with patch.object(execution_handler.requests, "post", return_value=resp):
            result = p._approve(["/exec", "approve", "job-123"])

        assert "qwen3:4b" in result, "model_used must be surfaced"
        assert "default" not in result.lower() or "qwen3:4b" in result, \
            "should not fall back to 'default' when model_used is present"

    def test_output_rendered_short(self):
        resp = _mock_resp(json_data={
            "status": "done",
            "node_key": "T1",
            "output": "Short inline output.",
            "model_used": "qwen3:4b",
        })
        p = _pipeline()
        with patch.object(execution_handler.requests, "post", return_value=resp):
            result = p._approve(["/exec", "approve", "job-123"])

        assert "Short inline output." in result
        assert "truncated" not in result

    def test_output_truncated_when_long(self):
        # Use 'Z' — doesn't appear in any template string in _approve output
        long_output = "Z" * 1500
        resp = _mock_resp(json_data={
            "status": "done",
            "node_key": "T1",
            "output": long_output,
            "model_used": "qwen3:4b",
        })
        p = _pipeline()
        with patch.object(execution_handler.requests, "post", return_value=resp):
            result = p._approve(["/exec", "approve", "job-123"])

        assert "900 chars truncated" in result, \
            "1500-600=900 chars should be reported as truncated"
        assert result.count("Z") == 600, "exactly 600 chars of content should survive"

    def test_verification_passed_shows_confidence(self):
        resp = _mock_resp(json_data={
            "status": "done",
            "node_key": "T1",
            "output": "ok",
            "model_used": "qwen3:4b",
            "verified": True,
            "confidence": 0.92,
        })
        p = _pipeline()
        with patch.object(execution_handler.requests, "post", return_value=resp):
            result = p._approve(["/exec", "approve", "job-123"])

        assert "Verified" in result
        assert "0.92" in result

    def test_verification_failed_shows_reason(self):
        resp = _mock_resp(json_data={
            "status": "done",
            "node_key": "T1",
            "output": "ok",
            "model_used": "qwen3:4b",
            "verified": False,
            "verification_reason": "output contradicts context",
        })
        p = _pipeline()
        with patch.object(execution_handler.requests, "post", return_value=resp):
            result = p._approve(["/exec", "approve", "job-123"])

        assert "Failed" in result or "⚠️" in result
        assert "contradicts context" in result


# ---------------------------------------------------------------------------
# #8.16 — defensive dict access in _status
# ---------------------------------------------------------------------------

class TestStatusDefensiveAccess:
    """#8.16: pipeline must not crash on partial/unexpected response shapes."""

    def test_missing_counts_does_not_crash(self):
        resp = _mock_resp(json_data={
            "job_id": "job-1",
            "job_title": "Test Job",
            "job_status": "running",
            "nodes": [],
            # counts intentionally missing
        })
        p = _pipeline()
        with patch.object(execution_handler.requests, "get", return_value=resp):
            result = p._status(["/exec", "status", "job-1"])

        assert "Test Job" in result
        assert "running" in result

    def test_empty_response_does_not_crash(self):
        resp = _mock_resp(json_data={})
        p = _pipeline()
        with patch.object(execution_handler.requests, "get", return_value=resp):
            result = p._status(["/exec", "status", "job-1"])

        # Should render with 'unknown'/defaults, not raise KeyError
        assert "unknown" in result or "?" in result

    def test_error_response_handled_gracefully(self):
        """execution_status() returns HTTP 200 with {'error': ...} on job-not-found."""
        resp = _mock_resp(json_data={"error": "Job xxx not found"})
        p = _pipeline()
        with patch.object(execution_handler.requests, "get", return_value=resp):
            result = p._status(["/exec", "status", "xxx"])

        assert "not found" in result
        assert "❌" in result

    def test_node_missing_fields_does_not_crash(self):
        resp = _mock_resp(json_data={
            "job_title": "T",
            "job_status": "running",
            "counts": {"pending": 1},
            "nodes": [{"node_key": "T1"}],  # missing status, execution_order, etc.
        })
        p = _pipeline()
        with patch.object(execution_handler.requests, "get", return_value=resp):
            result = p._status(["/exec", "status", "job-1"])

        assert "T1" in result


# ---------------------------------------------------------------------------
# #8.24 — non-JSON response handling
# ---------------------------------------------------------------------------

class TestNonJsonResponse:
    """#8.24: resp.json() exceptions must render a clear error, not a traceback."""

    def test_status_handles_nginx_502_html(self):
        resp = _mock_resp(
            status_code=502,
            text="<html><body>502 Bad Gateway</body></html>",
            raise_on_json=True,
        )
        p = _pipeline()
        with patch.object(execution_handler.requests, "get", return_value=resp):
            result = p._status(["/exec", "status", "job-1"])

        assert "non-JSON" in result
        assert "502" in result

    def test_approve_handles_nginx_502_html(self):
        resp = _mock_resp(
            status_code=502,
            text="<html>Bad Gateway</html>",
            raise_on_json=True,
        )
        p = _pipeline()
        with patch.object(execution_handler.requests, "post", return_value=resp):
            result = p._approve(["/exec", "approve", "job-1"])

        assert "non-JSON" in result
        assert "502" in result

    def test_skip_handles_non_json(self):
        resp = _mock_resp(status_code=500, text="internal error", raise_on_json=True)
        p = _pipeline()
        with patch.object(execution_handler.requests, "post", return_value=resp):
            result = p._skip(["/exec", "skip", "job-1", "T1"])

        assert "non-JSON" in result

    def test_retry_handles_non_json(self):
        resp = _mock_resp(status_code=500, text="internal error", raise_on_json=True)
        p = _pipeline()
        with patch.object(execution_handler.requests, "post", return_value=resp):
            result = p._retry(["/exec", "retry", "job-1", "T1"])

        assert "non-JSON" in result


# ---------------------------------------------------------------------------
# _skip / _retry response-shape handling
# ---------------------------------------------------------------------------

class TestSkipResponseShape:
    """_skip must not claim success when orchestrator returned an error dict."""

    def test_success_branch(self):
        resp = _mock_resp(json_data={"status": "skipped", "node_key": "T1"})
        p = _pipeline()
        with patch.object(execution_handler.requests, "post", return_value=resp):
            result = p._skip(["/exec", "skip", "job-1", "T1"])

        assert "skipped" in result
        assert "❌" not in result

    def test_error_branch_not_silently_reported_as_success(self):
        resp = _mock_resp(json_data={
            "status": "error", "message": "Node T99 not found",
        })
        p = _pipeline()
        with patch.object(execution_handler.requests, "post", return_value=resp):
            result = p._skip(["/exec", "skip", "job-1", "T99"])

        assert "❌" in result
        assert "not found" in result


class TestRetryResponseShape:
    def test_reset_branch(self):
        resp = _mock_resp(json_data={"status": "reset", "node_key": "T1"})
        p = _pipeline()
        with patch.object(execution_handler.requests, "post", return_value=resp):
            result = p._retry(["/exec", "retry", "job-1", "T1"])

        assert "reset to pending" in result

    def test_error_via_message_field(self):
        resp = _mock_resp(json_data={
            "status": "error", "message": "Node T99 not found",
        })
        p = _pipeline()
        with patch.object(execution_handler.requests, "post", return_value=resp):
            result = p._retry(["/exec", "retry", "job-1", "T99"])

        assert "❌" in result
        assert "not found" in result


# ---------------------------------------------------------------------------
# Connection failure
# ---------------------------------------------------------------------------

class TestConnectionErrors:
    def test_status_connection_error_rendered(self):
        p = _pipeline()
        with patch.object(
            execution_handler.requests, "get",
            side_effect=requests.exceptions.ConnectionError("refused"),
        ):
            result = p._status(["/exec", "status", "job-1"])

        assert "Connection error" in result
        assert "refused" in result


# ---------------------------------------------------------------------------
# Usage validation
# ---------------------------------------------------------------------------

class TestUsageMessages:
    def test_approve_missing_job_id(self):
        p = _pipeline()
        assert "Usage" in p._approve(["/exec", "approve"])

    def test_skip_missing_node_key(self):
        p = _pipeline()
        assert "Usage" in p._skip(["/exec", "skip", "job-1"])

    def test_retry_missing_node_key(self):
        p = _pipeline()
        assert "Usage" in p._retry(["/exec", "retry", "job-1"])


# ---------------------------------------------------------------------------
# Valve defaults (#8.15)
# ---------------------------------------------------------------------------

class TestValves:
    def test_request_timeout_default(self):
        p = Pipeline()
        assert p.valves.request_timeout == 310
