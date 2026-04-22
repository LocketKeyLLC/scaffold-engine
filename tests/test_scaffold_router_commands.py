"""Tests for scaffold_router.py — command-handling flows (/go, /confirm, /model, /research, /schedule).

Split from the original test_scaffold_router.py (#9.6).
Shared module-loading logic lives in _scaffold_router_setup.py.
"""
import json
from unittest.mock import MagicMock, patch

import pytest

from tests._scaffold_router_setup import _mod, Pipeline, _make_response


@pytest.fixture
def pipe():
    """Fresh Pipeline instance per test."""
    return Pipeline()


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



    @patch("pipelines.scaffold_router.requests.post")
    def test_confirm_invokes_execute_all(self, mock_post, pipe):
        """/confirm must auto-chain into /execute/all (regression for known issue #14)."""
        from unittest.mock import MagicMock
        confirm_resp = MagicMock(status_code=200)
        confirm_resp.json.return_value = {"status": "planning", "job_id": "job-42"}
        dag_resp = MagicMock(status_code=200)
        dag_resp.json.return_value = {"task_count": 3, "tasks": []}
        exec_resp = MagicMock(status_code=200)
        exec_resp.iter_lines.return_value = iter([])
        exec_resp.close = MagicMock()
        mock_post.side_effect = [confirm_resp, dag_resp, exec_resp]
        messages = [{"role": "user", "content": "/confirm job-42"}]
        list(pipe.pipe("/confirm job-42", "test-model", messages, {}))
        urls = [c.args[0] for c in mock_post.call_args_list]
        assert any("/ideate/confirm" in u for u in urls)
        assert any("/dag" in u for u in urls)
        assert any("/execute/all" in u for u in urls), f"/execute/all never called — got {urls}"


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


# ===================================================================
# /research command tests
# ===================================================================


class TestResearchCommand:
    """Tests for /research command parsing and dispatch."""

    def test_research_usage_error(self, pipe):
        """'/research' with no topic yields usage message."""
        output = list(pipe.pipe(
            user_message="/research",
            model_id="test",
            messages=[{"role": "user", "content": "/research"}],
            body={"messages": [{"role": "user", "content": "/research"}]},
        ))
        combined = "".join(output)
        assert "Usage" in combined
        assert "/research <topic>" in combined

    def test_research_depth_flag_parsed(self, pipe):
        """'--depth deep' is extracted and topic is cleaned."""
        called_with = {}

        def fake_stream(topic, depth):
            called_with["topic"] = topic
            called_with["depth"] = depth
            yield "done"

        pipe._research_and_stream = fake_stream
        output = list(pipe.pipe(
            user_message="/research Docker networking --depth deep",
            model_id="test",
            messages=[{"role": "user", "content": "/research Docker networking --depth deep"}],
            body={"messages": [{"role": "user", "content": "/research Docker networking --depth deep"}]},
        ))
        assert called_with["topic"] == "Docker networking"
        assert called_with["depth"] == "deep"

    def test_research_default_depth_medium(self, pipe):
        """No --depth flag defaults to 'medium'."""
        called_with = {}

        def fake_stream(topic, depth):
            called_with["topic"] = topic
            called_with["depth"] = depth
            yield "done"

        pipe._research_and_stream = fake_stream
        output = list(pipe.pipe(
            user_message="/research Python asyncio patterns",
            model_id="test",
            messages=[{"role": "user", "content": "/research Python asyncio patterns"}],
            body={"messages": [{"role": "user", "content": "/research Python asyncio patterns"}]},
        ))
        assert called_with["depth"] == "medium"
        assert called_with["topic"] == "Python asyncio patterns"

    def test_research_stream_triggered(self, pipe):
        """Valid /research triggers header + _research_and_stream."""
        stream_called = [False]

        def fake_stream(topic, depth):
            stream_called[0] = True
            yield "streaming..."

        pipe._research_and_stream = fake_stream
        output = list(pipe.pipe(
            user_message="/research Redis caching",
            model_id="test",
            messages=[{"role": "user", "content": "/research Redis caching"}],
            body={"messages": [{"role": "user", "content": "/research Redis caching"}]},
        ))
        combined = "".join(output)
        assert stream_called[0] is True
        assert "Researching" in combined
        assert "Redis caching" in combined

    def test_research_complete_suggests_go(self, pipe, monkeypatch):
        """After research_complete, output should prompt the user to type /go."""
        class FakeResponse:
            status_code = 200
            def iter_lines(self, decode_unicode=True):
                yield "event: research_complete"
                yield (
                    'data: {"topic": "Docker networking", "total_ingested": 12, '
                    '"total_entries": 15, "iterations": 2, "duration_ms": 120000, '
                    '"summary": "Docker uses bridge networks by default."}'
                )
                yield ""
            def close(self):
                pass

        monkeypatch.setattr(
            _mod.requests, "post", lambda *a, **kw: FakeResponse()
        )

        output = "".join(pipe._research_and_stream("Docker networking", "medium"))
        assert "/go" in output
        assert "build a project plan" in output
        assert "Research Complete" in output

    def test_awaiting_reply_renders_paused_block(self, pipe, monkeypatch):
        """awaiting_reply SSE event renders the pause block with session id + hints."""
        class FakeResponse:
            status_code = 200
            def iter_lines(self, decode_unicode=True):
                yield "event: awaiting_reply"
                yield (
                    'data: {"session_id": "sess_abc123", '
                    '"question": "Do you mean Docker Swarm or Kubernetes?", '
                    '"topic": "container orchestration", '
                    '"iteration": 2, "expires_in_seconds": 3600}'
                )
                yield ""
            def close(self):
                pass
        monkeypatch.setattr(
            _mod.requests, "post", lambda *a, **kw: FakeResponse()
        )
        output = "".join(pipe._research_and_stream("container orchestration", "medium"))
        # Pause header rendered
        assert "Research paused" in output
        # Question surfaced
        assert "Do you mean Docker Swarm or Kubernetes?" in output
        # Session ID shown so user can copy it
        assert "sess_abc123" in output
        # Resume hint with the exact command
        assert "/research/reply sess_abc123" in output
        # Expiry converted to minutes (3600s = 60 min)
        assert "60 min" in output

    def test_research_reply_posts_to_reply_endpoint(self, pipe, monkeypatch):
        """/research/reply dispatch posts to /research/reply with session_id + reply."""
        captured = {}
        class FakeResponse:
            status_code = 200
            def iter_lines(self, decode_unicode=True):
                yield "event: research_resumed"
                yield 'data: {"session_id": "sess_xyz", "reply": "Kubernetes", "iteration": 3}'
                yield ""
            def close(self):
                pass
        def fake_post(url, *a, **kw):
            captured["url"] = url
            captured["json"] = kw.get("json", {})
            return FakeResponse()
        monkeypatch.setattr(_mod.requests, "post", fake_post)

        output = "".join(
            pipe._research_reply_and_stream("sess_xyz", "Kubernetes")
        )

        # Posted to the reply endpoint, not /research
        assert captured["url"].endswith("/research/reply")
        # Correct schema: session_id + reply (not job_id / not answer)
        assert captured["json"].get("session_id") == "sess_xyz"
        assert captured["json"].get("reply") == "Kubernetes"
        # Resume event rendered to chat
        assert "Resuming session" in output
        assert "sess_xyz" in output


# =======================================================================
# Phase 7 additions — #8.1, #8.2, #8.6, #8.8, #8.9, #8.10, #8.11, #8.12
# =======================================================================

import queue as _queue


@pytest.mark.smoke
class TestWordBoundaryCommands:
    """#8.6: /executor must NOT match /exec; /confirmation must NOT match /confirm."""

    def test_executor_does_not_match_exec(self, pipe):
        assert pipe._is_cmd("/executor foo", "/exec") is False
        assert pipe._is_cmd("/executor", "/exec") is False

    def test_exec_matches_exec(self, pipe):
        assert pipe._is_cmd("/exec job_123", "/exec") is True
        assert pipe._is_cmd("/exec", "/exec") is True

    def test_confirmation_does_not_match_confirm(self, pipe):
        assert pipe._is_cmd("/confirmation yes", "/confirm") is False

    def test_confirm_matches_confirm(self, pipe):
        assert pipe._is_cmd("/confirm job_123", "/confirm") is True

    def test_executor_falls_through_all_commands(self, pipe):
        known = ("/exec", "/execute", "/confirm", "/go", "/run",
                 "/research", "/research/reply", "/dag", "/idea",
                 "/skip", "/optimize", "/rag", "/status", "/model",
                 "/schedule", "/results", "/help", "/prompt")
        assert not any(pipe._is_cmd("/executor please", c) for c in known)


@pytest.mark.smoke
class TestResultsCommand:
    """#8.1: /results <job_id> renders output/progress/error from /exec/status."""

    def test_completed_renders_compiled_output(self, pipe):
        with patch("scaffold_router.requests.get") as mg:
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "status": "completed",
                "compiled_output": "## Final Report\n\nHere is the output.",
            }
            mg.return_value = resp
            out = pipe._handle_results(["/results", "job_abc"])
        assert "Final Report" in out
        assert "Here is the output." in out

    def test_404_returns_not_found(self, pipe):
        with patch("scaffold_router.requests.get") as mg:
            mg.return_value = MagicMock(status_code=404, text="")
            out = pipe._handle_results(["/results", "bogus"])
        assert "Job not found" in out
        assert "bogus" in out

    def test_running_shows_progress(self, pipe):
        with patch("scaffold_router.requests.get") as mg:
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "status": "running",
                "total_nodes": 7,
                "completed_nodes": 3,
                "current_node": {"node_key": "T4", "title": "write_report"},
            }
            mg.return_value = resp
            out = pipe._handle_results(["/results", "j"])
        assert "running" in out
        assert "3/7" in out
        assert "T4" in out
        assert "write_report" in out

    def test_failed_shows_error(self, pipe):
        with patch("scaffold_router.requests.get") as mg:
            resp = MagicMock(status_code=200)
            resp.json.return_value = {
                "status": "failed",
                "error_summary": "verifier rejected T3 after max retries",
            }
            mg.return_value = resp
            out = pipe._handle_results(["/results", "j"])
        assert "failed" in out
        assert "verifier rejected" in out

    def test_dispatch_via_handle_command(self, pipe):
        with patch("scaffold_router.requests.get") as mg:
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"status": "completed", "compiled_output": "DONE"}
            mg.return_value = resp
            out = pipe._handle_command("/results job_xyz")
        assert "DONE" in out


@pytest.mark.smoke
class TestScheduleDepthFlag:
    """#8.9: /schedule add parses --depth and sends it in payload."""

    def test_depth_equals_syntax(self, pipe):
        captured = {}
        def _mp(url, **kw):
            captured["json"] = kw.get("json")
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"id": 1, "topic": "research foo",
                                      "cron_expression": "0 9 * * 1", "depth": "deep"}
            return resp
        with patch("scaffold_router.requests.post", side_effect=_mp):
            out = pipe._handle_schedule('/schedule add "0 9 * * 1" --depth=deep research foo')
        assert captured["json"]["depth"] == "deep"
        assert captured["json"]["topic"] == "research foo"
        assert captured["json"]["cron_expression"] == "0 9 * * 1"

    def test_depth_space_syntax(self, pipe):
        captured = {}
        def _mp(url, **kw):
            captured["json"] = kw.get("json")
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"id": 2, "topic": "my topic",
                                      "cron_expression": "* * * * *", "depth": "medium"}
            return resp
        with patch("scaffold_router.requests.post", side_effect=_mp):
            pipe._handle_schedule('/schedule add "* * * * *" --depth medium my topic')
        assert captured["json"]["depth"] == "medium"

    def test_default_depth_shallow(self, pipe):
        captured = {}
        def _mp(url, **kw):
            captured["json"] = kw.get("json")
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"id": 3, "topic": "foo",
                                      "cron_expression": "0 0 * * *", "depth": "shallow"}
            return resp
        with patch("scaffold_router.requests.post", side_effect=_mp):
            pipe._handle_schedule('/schedule add "0 0 * * *" foo')
        assert captured["json"]["depth"] == "shallow"

    def test_invalid_depth_value(self, pipe):
        out = pipe._handle_schedule('/schedule add "* * * * *" --depth=insane topic')
        assert "Invalid" in out or "insane" in out

