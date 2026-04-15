"""
Test suite for scaffold_router.py — the "front desk receptionist" of Scaffold Engine.

Every user message passes through this file, so bugs here affect every interaction.
These tests verify the internal helper functions work correctly.

Organized in order of complexity:
  1. Pure functions (no external calls)
  2. Generator functions (produce output piece by piece)
  3. Formatter (needs a fake HTTP response, but no real network)
  4. HTTP-calling functions (need mocked network calls)
"""

import json
import sys
import importlib.util
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Import the Pipeline class directly from the pipelines directory.
# We use importlib so the test works whether run from the repo root,
# from inside Docker, or from CI — no matter where the file lives.
# ---------------------------------------------------------------------------
_router_candidates = [
    Path(__file__).resolve().parent.parent / "pipelines" / "scaffold_router.py",
    Path("/app/pipelines/scaffold_router.py"),
]

_router_path = None
for _p in _router_candidates:
    if _p.exists():
        _router_path = _p
        break

if _router_path is None:
    pytest.skip(
        "scaffold_router.py not found — skipping (expected in pipelines/ directory)",
        allow_module_level=True,
    )

spec = importlib.util.spec_from_file_location("scaffold_router", _router_path)
_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(_mod)
Pipeline = _mod.Pipeline


# ---------------------------------------------------------------------------
# Fixture: a fresh Pipeline instance for each test
# ---------------------------------------------------------------------------
@pytest.fixture
def pipe():
    """Create a fresh Pipeline instance — no framework needed."""
    return Pipeline()


# ===================================================================
# STEP 1: Pure functions — no external calls, easy to verify
# ===================================================================


# --- _extract_text ---------------------------------------------------
# This function pulls plain text out of message content.
# Content can be a simple string, a list of multimedia items
# (like images + text), None, or an empty list.

@pytest.mark.smoke
class TestExtractText:
    """_extract_text: pulls plain text from message content."""

    def test_string_input(self, pipe):
        """When content is already a plain string, return it as-is."""
        assert pipe._extract_text("hello world") == "hello world"

    def test_multimodal_list(self, pipe):
        """When content is a list of items (text + images), extract only the text."""
        content = [
            {"type": "text", "text": "Build a homelab"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
            {"type": "text", "text": "with Proxmox"},
        ]
        result = pipe._extract_text(content)
        assert "Build a homelab" in result
        assert "with Proxmox" in result
        # Image data should NOT appear in the extracted text
        assert "base64" not in result

    def test_none_input(self, pipe):
        """When content is None (missing), return empty string."""
        assert pipe._extract_text(None) == ""

    def test_empty_list(self, pipe):
        """When content is an empty list, return empty string."""
        assert pipe._extract_text([]) == ""

    def test_numeric_input(self, pipe):
        """When content is something unexpected like a number, stringify it."""
        result = pipe._extract_text(42)
        assert "42" in result


# --- _clean_messages -------------------------------------------------
# This function prepares conversation history for the AI model:
# - Strips invisible "zero-width space" characters that Open WebUI injects
# - Converts multimodal content lists to plain text
# - Drops messages that become empty after cleaning

@pytest.mark.smoke
class TestCleanMessages:
    """_clean_messages: sanitizes conversation history."""

    def test_strips_zero_width_spaces(self, pipe):
        """Zero-width spaces (invisible characters) should be removed."""
        messages = [{"role": "user", "content": "hello\u200b world\u200b"}]
        result = pipe._clean_messages(messages)
        assert len(result) == 1
        assert "\u200b" not in result[0]["content"]
        assert result[0]["content"] == "hello world"

    def test_drops_empty_messages(self, pipe):
        """Messages that are blank (or only zero-width spaces) get removed."""
        messages = [
            {"role": "user", "content": "real message"},
            {"role": "assistant", "content": "\u200b"},  # invisible-only → dropped
            {"role": "user", "content": ""},              # empty → dropped
        ]
        result = pipe._clean_messages(messages)
        assert len(result) == 1
        assert result[0]["content"] == "real message"

    def test_handles_multimodal_content(self, pipe):
        """Multimodal content lists should be flattened to plain text."""
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Check this diagram"},
                    {"type": "image_url", "image_url": {"url": "data:..."}},
                ],
            }
        ]
        result = pipe._clean_messages(messages)
        assert len(result) == 1
        assert result[0]["content"] == "Check this diagram"

    def test_preserves_role(self, pipe):
        """The role (user/assistant) should be preserved after cleaning."""
        messages = [
            {"role": "user", "content": "question"},
            {"role": "assistant", "content": "answer"},
        ]
        result = pipe._clean_messages(messages)
        assert result[0]["role"] == "user"
        assert result[1]["role"] == "assistant"


# --- _help -----------------------------------------------------------
# Returns the help text shown when a user types /help.
# We just verify it contains the key command names.

@pytest.mark.smoke
class TestHelp:
    """_help: returns the command reference text."""

    def test_returns_string(self, pipe):
        result = pipe._help()
        assert isinstance(result, str)

    def test_contains_key_commands(self, pipe):
        """Help text should mention all important commands."""
        result = pipe._help()
        for cmd in ["/go", "/idea", "/dag", "/execute", "/confirm",
                     "/status", "/help", "/rag", "/optimize", "/skip"]:
            assert cmd in result, f"Help text missing {cmd}"

    def test_contains_workflow_guidance(self, pipe):
        """Help text should explain the basic workflow."""
        result = pipe._help()
        # Should mention the workflow concept somewhere
        assert "workflow" in result.lower() or "triage" in result.lower()


# ===================================================================
# STEP 2: Generator function — produces output piece by piece
# ===================================================================

# _handle_sse_event parses real-time progress updates from the
# orchestrator (like "Step T1 started", "Step T2 failed") and
# turns them into user-friendly chat messages.

@pytest.mark.smoke
class TestHandleSSEEvent:
    """_handle_sse_event: converts raw progress events into chat messages."""

    def test_node_start(self, pipe):
        """A 'node_start' event should produce a progress message with the step name."""
        data = json.dumps({
            "node_key": "T1",
            "title": "Research phase",
            "tool": "SearXNG",
        })
        failed = []
        chunks = list(pipe._handle_sse_event("node_start", data, failed))
        assert len(chunks) == 1
        assert "T1" in chunks[0]
        assert "Research phase" in chunks[0]
        assert len(failed) == 0  # no failures recorded

    def test_node_done(self, pipe):
        """A 'node_done' event should show the step completed."""
        data = json.dumps({"node_key": "T1"})
        failed = []
        chunks = list(pipe._handle_sse_event("node_done", data, failed))
        assert len(chunks) == 1
        assert "T1" in chunks[0]
        assert "complete" in chunks[0].lower() or "✅" in chunks[0]

    def test_node_failed(self, pipe):
        """A 'node_failed' event should show the error AND record the failure."""
        data = json.dumps({
            "node_key": "T2",
            "error": "Model timeout",
        })
        failed = []
        chunks = list(pipe._handle_sse_event("node_failed", data, failed))
        assert len(chunks) == 1
        assert "T2" in chunks[0]
        assert "Model timeout" in chunks[0]
        # The failure should be recorded in the list
        assert len(failed) == 1

    def test_node_failed_with_verification_reason(self, pipe):
        """Failures can also have a 'verification_reason' instead of 'error'."""
        data = json.dumps({
            "node_key": "T3",
            "verification_reason": "Output did not match requirements",
        })
        failed = []
        chunks = list(pipe._handle_sse_event("node_failed", data, failed))
        assert "Output did not match requirements" in chunks[0]

    def test_blocked(self, pipe):
        """A 'blocked' event means a step is waiting for another step to finish."""
        data = json.dumps({
            "node_key": "T3",
            "blocked_by": ["T1", "T2"],
        })
        failed = []
        chunks = list(pipe._handle_sse_event("blocked", data, failed))
        assert len(chunks) == 1
        assert "T3" in chunks[0]
        assert "T1" in chunks[0]

    def test_node_retry(self, pipe):
        """A 'node_retry' event should show retry info to the user."""
        data = json.dumps({
            "job_id": "job-1",
            "node_key": "T1",
            "title": "Research phase",
            "retry_count": 2,
            "message": "Auto-retrying failed node",
        })
        failed = []
        chunks = list(pipe._handle_sse_event("node_retry", data, failed))
        assert len(chunks) == 1
        assert "T1" in chunks[0]
        assert "Retrying" in chunks[0]
        assert "attempt 2" in chunks[0]
        assert len(failed) == 0  # retries are not failures

    def test_pipeline_complete(self, pipe):
        """A 'pipeline_complete' event produces no output (handled elsewhere)."""
        data = json.dumps({"status": "completed"})
        failed = []
        chunks = list(pipe._handle_sse_event("pipeline_complete", data, failed))
        assert len(chunks) == 0

    def test_invalid_json(self, pipe):
        """If the event data is garbled, produce no output (don't crash)."""
        failed = []
        chunks = list(pipe._handle_sse_event("node_start", "not json!", failed))
        assert len(chunks) == 0


# ===================================================================
# STEP 3: Formatter — needs a fake HTTP response object
# ===================================================================

# _fmt takes an HTTP response from the orchestrator and formats it
# for display in the chat. We create a fake response object to test
# without making real network calls.

def _make_response(status_code: int, body: dict | str = "") -> MagicMock:
    """Create a fake requests.Response for testing _fmt."""
    r = MagicMock()
    r.status_code = status_code
    if isinstance(body, dict):
        r.json.return_value = body
        r.text = json.dumps(body)
    else:
        r.json.side_effect = ValueError("No JSON")
        r.text = body
    return r


@pytest.mark.smoke
class TestFmt:
    """_fmt: formats HTTP responses for chat display."""

    def test_successful_json(self, pipe):
        """A successful JSON response should be displayed as formatted JSON."""
        r = _make_response(200, {"job_id": "abc-123", "status": "planning"})
        result = pipe._fmt(r)
        assert "abc-123" in result
        assert "```json" in result  # wrapped in a code block

    def test_error_response(self, pipe):
        """An error response should show the error message clearly."""
        r = _make_response(400, {"detail": "Job not found"})
        result = pipe._fmt(r)
        assert "Job not found" in result
        assert "⚠️" in result or "Error" in result

    def test_non_json_response(self, pipe):
        """If the response isn't JSON (plain text), show it with the status code."""
        r = _make_response(502, "Bad Gateway")
        result = pipe._fmt(r)
        assert "502" in result
        assert "Bad Gateway" in result


# ===================================================================
# STEP 4: HTTP-calling functions — need mocked network calls
# ===================================================================

# _handle_command dispatches slash commands by making HTTP requests
# to the orchestrator. We "mock" (fake) the requests library so no
# real network calls happen during testing.

@pytest.mark.smoke
class TestHandleCommand:
    """_handle_command: dispatches slash commands via HTTP."""

    @patch("requests.get")
    def test_status_command(self, mock_get, pipe):
        """'/status' should call the orchestrator's /status endpoint."""
        mock_get.return_value = _make_response(200, {
            "active_jobs": [{"job_id": "abc-123", "status": "executing"}],
        })
        result = pipe._handle_command("/status")

        # Verify the right endpoint was called
        mock_get.assert_called_once()
        call_url = mock_get.call_args[0][0] if mock_get.call_args[0] else mock_get.call_args[1].get("url", "")
        if not call_url:
            # positional or keyword
            call_url = str(mock_get.call_args)
        assert "/status" in call_url or "/status" in str(mock_get.call_args)

        # Verify the response was formatted
        assert "abc-123" in result

    def test_unknown_command(self, pipe):
        """An unrecognized command should return a helpful error (no HTTP call)."""
        result = pipe._handle_command("/xyzzy")
        assert "unknown" in result.lower() or "Unknown" in result
        assert "/help" in result

    def test_help_command(self, pipe):
        """'/help' should return help text without making any HTTP call."""
        result = pipe._handle_command("/help")
        assert "/go" in result
        assert "/status" in result

    @patch("requests.post")
    def test_idea_command(self, mock_post, pipe):
        """'/idea <text>' should POST to the /ideate endpoint."""
        mock_post.return_value = _make_response(200, {
            "job_id": "new-job-456",
            "status": "refining",
        })
        result = pipe._handle_command("/idea Build a homelab monitoring dashboard")

        mock_post.assert_called_once()
        # Verify the idea text was sent in the request body
        call_kwargs = mock_post.call_args
        sent_json = call_kwargs[1].get("json") or call_kwargs.kwargs.get("json", {})
        assert "homelab" in str(sent_json).lower() or "new-job-456" in result

    @patch("requests.post")
    def test_idea_command_missing_text(self, mock_post, pipe):
        """'/idea' with no text should return usage instructions, not crash."""
        result = pipe._handle_command("/idea")
        assert "usage" in result.lower() or "Usage" in result
        mock_post.assert_not_called()  # no HTTP call should be made

    @patch("requests.post")
    def test_rag_command(self, mock_post, pipe):
        """'/rag <query>' should query the knowledge base."""
        mock_post.return_value = _make_response(200, {
            "results": [{"text": "Proxmox is a hypervisor..."}],
        })
        result = pipe._handle_command("/rag what is proxmox")
        mock_post.assert_called_once()
        assert "Proxmox" in result or "```json" in result

    @patch("requests.get")
    def test_network_timeout(self, mock_get, pipe):
        """Network timeouts should produce a friendly error, not a crash."""
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.Timeout()
        result = pipe._handle_command("/status")
        assert "timed out" in result.lower() or "timeout" in result.lower()

    @patch("requests.get")
    def test_connection_error(self, mock_get, pipe):
        """Connection failures should produce a friendly error."""
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.ConnectionError()
        result = pipe._handle_command("/status")
        assert "cannot reach" in result.lower() or "orchestrator" in result.lower()



# ===================================================================
# STEP 5: Command flow tests — /go, /confirm, context stripping
# ===================================================================

@pytest.mark.smoke
class TestGoCommand:
    """/go command flow in pipe()."""

    def test_go_with_empty_history(self, pipe):
        """If no user messages exist in history, /go yields Nothing to launch yet."""
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "/go"},
        ]
        body = {}
        chunks = list(pipe.pipe("/go", "test-model", messages, body))
        combined = "".join(chunks)
        assert "Nothing to launch yet" in combined

    def test_go_triggers_synthesis(self, pipe):
        """When history exists, /go calls _synthesize_idea and proceeds."""
        messages = [
            {"role": "user", "content": "Build a homelab dashboard"},
            {"role": "assistant", "content": "Sounds good."},
            {"role": "user", "content": "/go"},
        ]
        body = {}
        with patch.object(pipe, "_synthesize_idea", return_value="Build a homelab dashboard for Docker") as mock_synth, \
             patch.object(_mod, "requests") as mock_requests:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "job_id": "job-123",
                "status": "awaiting_confirmation",
                "confidence": 0.85,
                "risks": [],
                "clarifications": [],
            }
            mock_requests.post.return_value = mock_resp
            chunks = list(pipe.pipe("/go", "test-model", messages, body))
        mock_synth.assert_called_once()
        combined = "".join(chunks)
        assert "Synthesizing" in combined


@pytest.mark.smoke
class TestConfirmCommand:
    """/confirm command flow in pipe()."""

    def test_confirm_usage_error(self, pipe):
        """/confirm with no job_id yields usage message."""
        messages = [{"role": "user", "content": "/confirm"}]
        body = {}
        chunks = list(pipe.pipe("/confirm", "test-model", messages, body))
        combined = "".join(chunks)
        assert "Usage" in combined
        assert "/confirm" in combined


@pytest.mark.smoke
class TestContextStripping:
    """Open WebUI injects <context>...</context> before the real command."""

    def test_context_tags_stripped(self, pipe):
        """Command buried after </context> is extracted and /go is recognized."""
        raw_msg = "<context>some file content here</context>" + chr(10) + "/go"
        messages = [
            {"role": "user", "content": raw_msg},
        ]
        body = {}
        with patch.object(pipe, "_synthesize_idea", return_value="Test idea from context") as mock_synth, \
             patch.object(_mod, "requests") as mock_requests:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "job_id": "j1", "status": "awaiting_confirmation",
                "confidence": 0.9, "risks": [], "clarifications": [],
            }
            mock_requests.post.return_value = mock_resp
            chunks = list(pipe.pipe(raw_msg, "test-model", messages, body))
        # Context stripping worked — /go was recognized and synthesis ran
        mock_synth.assert_called_once()
        combined = "".join(chunks)
        assert "Synthesizing" in combined

    def test_no_context_tags_passes_through(self, pipe):
        """Message without context tags is used as-is."""
        messages = [{"role": "user", "content": "/help"}]
        body = {}
        chunks = list(pipe.pipe("/help", "test-model", messages, body))
        combined = "".join(chunks)
        assert "/go" in combined


# ===================================================================
# STEP 5: Command flow tests — /go, /confirm, context stripping
# ===================================================================

@pytest.mark.smoke
class TestGoCommand:
    """/go command flow in pipe()."""

    def test_go_with_empty_history(self, pipe):
        """If no user messages exist in history, /go yields Nothing to launch yet."""
        messages = [
            {"role": "system", "content": "You are helpful"},
            {"role": "user", "content": "/go"},
        ]
        body = {}
        chunks = list(pipe.pipe("/go", "test-model", messages, body))
        combined = "".join(chunks)
        assert "Nothing to launch yet" in combined

    def test_go_triggers_synthesis(self, pipe):
        """When history exists, /go calls _synthesize_idea and proceeds."""
        messages = [
            {"role": "user", "content": "Build a homelab dashboard"},
            {"role": "assistant", "content": "Sounds good."},
            {"role": "user", "content": "/go"},
        ]
        body = {}
        with patch.object(pipe, "_synthesize_idea", return_value="Build a homelab dashboard for Docker") as mock_synth, \
             patch.object(_mod, "requests") as mock_requests:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "job_id": "job-123",
                "status": "awaiting_confirmation",
                "confidence": 0.85,
                "risks": [],
                "clarifications": [],
            }
            mock_requests.post.return_value = mock_resp
            chunks = list(pipe.pipe("/go", "test-model", messages, body))
        mock_synth.assert_called_once()
        combined = "".join(chunks)
        assert "Synthesizing" in combined


@pytest.mark.smoke
class TestConfirmCommand:
    """/confirm command flow in pipe()."""

    def test_confirm_usage_error(self, pipe):
        """/confirm with no job_id yields usage message."""
        messages = [{"role": "user", "content": "/confirm"}]
        body = {}
        chunks = list(pipe.pipe("/confirm", "test-model", messages, body))
        combined = "".join(chunks)
        assert "Usage" in combined
        assert "/confirm" in combined


@pytest.mark.smoke
class TestContextStripping:
    """Open WebUI injects <context>...</context> before the real command."""

    def test_context_tags_stripped(self, pipe):
        """Command buried after </context> is extracted and /go is recognized."""
        raw_msg = '<context>some file content here</context>' + chr(10) + '/go'
        messages = [
            {"role": "user", "content": raw_msg},
        ]
        body = {}
        with patch.object(pipe, "_synthesize_idea", return_value="Test idea from context") as mock_synth, \
             patch.object(_mod, "requests") as mock_requests:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "job_id": "j1", "status": "awaiting_confirmation",
                "confidence": 0.9, "risks": [], "clarifications": [],
            }
            mock_requests.post.return_value = mock_resp
            chunks = list(pipe.pipe(raw_msg, "test-model", messages, body))
        mock_synth.assert_called_once()
        combined = "".join(chunks)
        assert "Synthesizing" in combined

    def test_no_context_tags_passes_through(self, pipe):
        """Message without context tags is used as-is."""
        messages = [{"role": "user", "content": "/help"}]
        body = {}
        chunks = list(pipe.pipe("/help", "test-model", messages, body))
        combined = "".join(chunks)
        assert "/go" in combined


# ===================================================================
# STEP 7: /model command system
# ===================================================================
@pytest.mark.smoke
class TestModelCommand:
    """_handle_model: /model list, set, reset, available, help."""

    def test_model_help(self, pipe):
        """'/model help' returns help text mentioning all 8 roles."""
        result = pipe._handle_model("/model help")
        for role in ("general", "verifier", "coder", "embedder",
                     "reranker", "router", "fallback", "cloud_alt"):
            assert role in result, f"Missing role '{role}' in help output"

    def test_model_list(self, pipe):
        """'/model list' returns a markdown table with all role assignments."""
        result = pipe._handle_model("/model list")
        assert "Current Model Assignments" in result
        assert "| Role |" in result
        # All 8 roles present
        for role in ("general", "verifier", "coder", "embedder",
                     "reranker", "router", "fallback", "cloud_alt"):
            assert role in result

    @patch("requests.get")
    def test_model_set_valid(self, mock_get, pipe):
        """'/model set general qwen3:8b' with valid Ollama response updates the valve."""
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": "qwen3:8b"}, {"name": "qwen2.5:7b"}]},
            raise_for_status=lambda: None,
        )
        result = pipe._handle_model("/model set general qwen3:8b")
        assert "Updated" in result
        assert "qwen3:8b" in result
        assert pipe.valves.model_general == "qwen3:8b"

    def test_model_set_invalid_role(self, pipe):
        """'/model set bogus qwen3:8b' returns error listing valid roles."""
        result = pipe._handle_model("/model set bogus qwen3:8b")
        assert "Unknown role" in result
        assert "general" in result  # valid roles listed

    @patch("requests.get")
    def test_model_set_model_not_found(self, mock_get, pipe):
        """If model isn't on Ollama, return error without changing valve."""
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [{"name": "qwen2.5:7b"}]},
            raise_for_status=lambda: None,
        )
        old_val = pipe.valves.model_general
        result = pipe._handle_model("/model set general nonexistent:99b")
        assert "not found" in result
        assert pipe.valves.model_general == old_val  # unchanged

    def test_model_reset(self, pipe):
        """'/model reset' restores defaults and reports changes."""
        pipe.valves.model_general = "custom:13b"
        pipe.valves.model_verifier = "custom:1b"
        result = pipe._handle_model("/model reset")
        assert "Reset to defaults" in result
        assert "general" in result
        assert pipe.valves.model_general == "qwen3-vl:235b-instruct-cloud"
        assert pipe.valves.model_verifier == "qwen2.5:7b"

    @patch("requests.get")
    def test_model_available(self, mock_get, pipe):
        """'/model available' lists models from Ollama."""
        mock_get.return_value = MagicMock(
            status_code=200,
            json=lambda: {"models": [
                {"name": "qwen3:8b"},
                {"name": "qwen2.5:7b"},
                {"name": "llama3:8b"},
            ]},
            raise_for_status=lambda: None,
        )
        result = pipe._handle_model("/model available")
        assert "Available Ollama Models" in result
        assert "(3)" in result
        assert "qwen3:8b" in result
