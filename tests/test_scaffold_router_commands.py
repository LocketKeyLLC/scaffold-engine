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

    @patch("scaffold_router._HTTP_SESSION.get")
    def test_status_command(self, mock_get, pipe):
        """'/status' should call the orchestrator's /status endpoint.

        X.18 — pre-X.18 fixture used the legacy ``active_jobs`` shape;
        the live response carries ``recent_jobs`` + ``status_counts``
        + per-row title/next_actions since U.7's UX-gap audit. Updated
        the canned payload to match what _render_status now expects.
        """
        mock_get.return_value = _make_response(200, {
            "total_jobs": 1,
            "status_counts": {"executing": 1},
            "recent_jobs": [
                {"id": "abc-123", "status": "executing",
                 "title": "demo job", "node_count": 3,
                 "updated_at": "2026-05-08T00:00:00",
                 "next_actions": []},
            ],
        })
        result = pipe._handle_command("/status")

        # Verify the right endpoint was called
        mock_get.assert_called_once()
        assert "/status" in str(mock_get.call_args)

        # Verify the response was formatted — short-id (first 8 chars) is
        # what _render_status renders for each recent_jobs row.
        assert "abc-123"[:8] in result
        # Title also surfaces in the recent-jobs table.
        assert "demo job" in result

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

    @patch("scaffold_router._HTTP_SESSION.post")
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

    @patch("scaffold_router._HTTP_SESSION.post")
    def test_idea_command_missing_text(self, mock_post, pipe):
        """'/idea' with no text should return usage instructions, not crash."""
        result = pipe._handle_command("/idea")
        assert "usage" in result.lower() or "Usage" in result
        mock_post.assert_not_called()  # no HTTP call should be made

    @patch("scaffold_router._HTTP_SESSION.post")
    def test_rag_command(self, mock_post, pipe):
        """'/rag <query>' should query the knowledge base."""
        mock_post.return_value = _make_response(200, {
            "results": [{"text": "Proxmox is a hypervisor..."}],
        })
        result = pipe._handle_command("/rag what is proxmox")
        mock_post.assert_called_once()
        assert "Proxmox" in result or "```json" in result

    @patch("scaffold_router._HTTP_SESSION.post")
    def test_rag_renders_source_type_and_confidence(self, mock_post, pipe):
        """§17.215 E4 — /rag results render ``source_type`` and
        ``confidence_score`` per hit (populated since §17.104 + §17.120).
        """
        mock_post.return_value = _make_response(200, {
            "results": [
                {
                    "text": "Proxmox is a hypervisor...",
                    "source_type": "tech_docs",
                    "confidence_score": 0.82,
                    "source_url": "https://example.com/proxmox",
                },
                {
                    "text": "Second result body.",
                    "source_type": "chat_log",
                    "confidence_score": 0.41,
                },
            ],
        })
        result = pipe._handle_command("/rag what is proxmox")
        assert "source_type=tech_docs" in result
        assert "confidence=0.82" in result
        assert "source_type=chat_log" in result
        assert "confidence=0.41" in result
        # Both bodies still surfaced.
        assert "Proxmox is a hypervisor" in result
        assert "Second result body" in result

    @patch("scaffold_router._HTTP_SESSION.post")
    def test_rag_empty_results(self, mock_post, pipe):
        """§17.215 E4 — empty results array renders a friendly "no
        matches" line rather than an empty JSON dump.
        """
        mock_post.return_value = _make_response(200, {"results": []})
        result = pipe._handle_command("/rag obscure query")
        assert "No matches" in result
        assert "obscure query" in result

    @patch("scaffold_router._HTTP_SESSION.get")
    def test_skip_bare_lists_candidates(self, mock_get, pipe):
        """§17.215 E1 — bare ``/skip <job_id>`` fetches status and
        emits a markdown hint with copy-pasteable ``/skip job_id
        node_key`` lines for failed / blocked / pending nodes.
        """
        mock_get.return_value = _make_response(200, {
            "status": "failed",
            "nodes": [
                {"node_key": "T1", "title": "Setup", "status": "done"},
                {"node_key": "T2", "title": "Build artifact", "status": "failed"},
                {"node_key": "T3", "title": "Wait on T2", "status": "blocked"},
                {"node_key": "T4", "title": "Cleanup", "status": "pending"},
                {"node_key": "T5", "title": "Now running", "status": "running"},
            ],
        })
        result = pipe._handle_command("/skip 01ab243e")
        mock_get.assert_called_once()
        # Each candidate surfaces a copy-pasteable command line.
        assert "/skip 01ab243e T2" in result
        assert "/skip 01ab243e T3" in result
        assert "/skip 01ab243e T4" in result
        # Done / running nodes are excluded.
        assert "/skip 01ab243e T1" not in result
        assert "/skip 01ab243e T5" not in result

    @patch("scaffold_router._HTTP_SESSION.get")
    def test_skip_bare_no_candidates(self, mock_get, pipe):
        """§17.215 E1 — bare ``/skip <job_id>`` with no skippable
        nodes still emits the usage hint plus a clear "nothing to
        skip" line rather than an empty list.
        """
        mock_get.return_value = _make_response(200, {
            "status": "completed",
            "nodes": [
                {"node_key": "T1", "title": "Setup", "status": "done"},
            ],
        })
        result = pipe._handle_command("/skip 01ab243e")
        assert "no skippable nodes" in result.lower()
        assert "completed" in result

    @patch("scaffold_router._HTTP_SESSION.post")
    def test_skip_with_node_key_unchanged(self, mock_post, pipe):
        """§17.215 E1 — the full ``/skip <job_id> <node_key>`` form
        still POSTs to /skip exactly as before (no regression for
        scripted callers / muscle-memory operators).
        """
        mock_post.return_value = _make_response(200, {"status": "skipped"})
        result = pipe._handle_command("/skip 01ab243e T2")
        mock_post.assert_called_once()
        # Body sent to orchestrator carries both fields.
        call_kwargs = mock_post.call_args
        sent = call_kwargs.kwargs.get("json") or (
            call_kwargs.args[1] if len(call_kwargs.args) > 1 else {}
        )
        assert sent.get("job_id") == "01ab243e"
        assert sent.get("node_key") == "T2"

    @patch("scaffold_router._HTTP_SESSION.get")
    def test_network_timeout(self, mock_get, pipe):
        """Network timeouts should produce a friendly error, not a crash."""
        import requests as req_lib
        mock_get.side_effect = req_lib.exceptions.Timeout()
        result = pipe._handle_command("/status")
        assert "timed out" in result.lower() or "timeout" in result.lower()

    @patch("scaffold_router._HTTP_SESSION.get")
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
        with patch.object(pipe, "_synthesize_idea", return_value=("Build a homelab dashboard for Docker", False)) as mock_synth, \
             patch.object(_mod, "_HTTP_SESSION") as mock_requests:
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



    @patch("pipelines.scaffold_router._HTTP_SESSION.post")
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

    def test_confirm_into_assist_carries_chat_id(self, pipe, monkeypatch):
        """When valves.assist_after_confirm=True, /confirm auto-chains into
        /assist/start AND must plumb chat_id from body['metadata']['chat_id']
        so the W.9 chatmap PUT happens. Regression: pre-fix, the auto path
        called _assist_start(job_id) without chat_id, so OWUI users on the
        valve never got the implicit-session memory."""
        pipe.valves.assist_after_confirm = True
        log, responses = _http_call_log(monkeypatch)
        responses[("post", "/ideate/confirm")] = _make_response(
            200, {"status": "planning", "job_id": "job-77"},
        )
        responses[("post", "/dag")] = _make_response(
            200, {"task_count": 2, "tasks": []},
        )
        responses[("post", "/assist/start")] = _make_response(
            200, {"session_id": _UUID_A, "job_id": "job-77", "pending_steps": 2},
        )
        responses[("get", f"/assist/{_UUID_A}/next")] = _make_response(
            200, {"session_id": _UUID_A, "node_key": "T1", "title": "step",
                  "status": "active", "depends_on": []},
        )
        responses[("put", "/assist/_chatmap/")] = _make_response(200, {"stored": True})

        body = {"metadata": {"chat_id": "chat-confirm-into-assist"}}
        list(pipe.pipe("/confirm job-77", "m", [{"role": "user", "content": "/confirm job-77"}], body))

        puts = [
            e for e in log
            if e[0] == "put" and "_chatmap/chat-confirm-into-assist" in e[1]
        ]
        assert puts, (
            f"/confirm with assist_after_confirm=True must PUT to "
            f"/assist/_chatmap/<chat_id> after starting; got log: {log}"
        )
        assert puts[0][2]["session_id"] == _UUID_A


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
        with patch.object(pipe, "_synthesize_idea", return_value=("Test idea from context", False)) as mock_synth, \
             patch.object(_mod, "_HTTP_SESSION") as mock_requests:
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
# §17.200 — silent-fallback warning on /go
# ===================================================================

@pytest.mark.smoke
class TestSynthesisFallbackWarning:
    """When /go's triage LLM fails (transport / HTTP / empty), the
    pipeline silently used `" ".join(user_texts)` as the launch plan.
    §17.200 — caller now yields a visible "couldn't synthesize, using
    raw messages" warning before launching so the user knows their
    plan wasn't actually LLM-refined."""

    def test_warning_emitted_when_fallback_used(self, pipe):
        """``_synthesize_idea`` returns ``(text, True)`` on fallback;
        the /go caller yields the §17.200 warning before auto-chaining."""
        messages = [
            {"role": "user", "content": "Build a thing"},
            {"role": "user", "content": "/go"},
        ]
        body = {}
        with patch.object(
            pipe, "_synthesize_idea",
            return_value=("Build a thing", True),  # used_fallback=True
        ), patch.object(_mod, "_HTTP_SESSION") as mock_requests:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "job_id": "j1", "status": "awaiting_confirmation",
                "confidence": 0.9, "risks": [], "clarifications": [],
            }
            mock_requests.post.return_value = mock_resp
            chunks = list(pipe.pipe("/go", "test-model", messages, body))
        combined = "".join(chunks)
        assert "Couldn't synthesize" in combined
        assert "raw messages" in combined
        # The launch still proceeds — warning doesn't block.
        assert "Launching with" in combined

    def test_no_warning_when_synthesis_succeeded(self, pipe):
        """``_synthesize_idea`` returns ``(text, False)`` on success;
        the §17.200 warning must NOT fire — pre-§17.200 launches were
        silent on the happy path and that stays."""
        messages = [
            {"role": "user", "content": "Build a thing"},
            {"role": "user", "content": "/go"},
        ]
        body = {}
        with patch.object(
            pipe, "_synthesize_idea",
            return_value=("A nicely synthesized plan", False),
        ), patch.object(_mod, "_HTTP_SESSION") as mock_requests:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "job_id": "j1", "status": "awaiting_confirmation",
                "confidence": 0.9, "risks": [], "clarifications": [],
            }
            mock_requests.post.return_value = mock_resp
            chunks = list(pipe.pipe("/go", "test-model", messages, body))
        combined = "".join(chunks)
        assert "Couldn't synthesize" not in combined
        assert "Launching with" in combined


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

    @patch("scaffold_router._HTTP_SESSION.get")
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

    @patch("scaffold_router._HTTP_SESSION.get")
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

    @patch("scaffold_router._HTTP_SESSION.get")
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
        """'/research' with no topic yields the placeholder hint + parser
        examples.

        X.18 — pre-X.18 the test expected a literal "Usage" string, but
        the live parser shows a placeholder-rejection message + a bullet
        list of example invocations. The assertion now matches the
        real shape: the operator-facing hint identifies the missing
        topic and surfaces example invocations.
        """
        output = list(pipe.pipe(
            user_message="/research",
            model_id="test",
            messages=[{"role": "user", "content": "/research"}],
            body={"messages": [{"role": "user", "content": "/research"}]},
        ))
        combined = "".join(output)
        # Operator-facing message identifies the missing/placeholder topic.
        assert "topic is missing or a placeholder" in combined
        # Examples section surfaces at least the basic shape.
        assert "/research" in combined

    def test_research_depth_flag_parsed(self, pipe):
        """'--depth deep' is extracted and topic is cleaned.

        §17.215 E2 — short queries now hit the /rag vs /research
        disambiguation prompt unless --confirm is passed. ``Docker
        networking`` is 2 tokens (≤4), so the test now includes
        ``--confirm`` to keep the assertion on the parsed topic/depth.
        """
        called_with = {}

        def fake_stream(topic, depth):
            called_with["topic"] = topic
            called_with["depth"] = depth
            yield "done"

        pipe._research_and_stream = fake_stream
        output = list(pipe.pipe(
            user_message="/research Docker networking --depth deep --confirm",
            model_id="test",
            messages=[{"role": "user", "content": "/research Docker networking --depth deep --confirm"}],
            body={"messages": [{"role": "user", "content": "/research Docker networking --depth deep --confirm"}]},
        ))
        assert called_with["topic"] == "Docker networking"
        assert called_with["depth"] == "deep"

    def test_research_default_depth_medium(self, pipe):
        """No --depth flag defaults to 'medium'.

        §17.215 E2 — adds ``--confirm`` because ``Python asyncio
        patterns`` is 3 tokens (≤4) and would otherwise trip the
        disambiguation prompt before reaching _research_and_stream.
        """
        called_with = {}

        def fake_stream(topic, depth):
            called_with["topic"] = topic
            called_with["depth"] = depth
            yield "done"

        pipe._research_and_stream = fake_stream
        output = list(pipe.pipe(
            user_message="/research Python asyncio patterns --confirm",
            model_id="test",
            messages=[{"role": "user", "content": "/research Python asyncio patterns --confirm"}],
            body={"messages": [{"role": "user", "content": "/research Python asyncio patterns --confirm"}]},
        ))
        assert called_with["depth"] == "medium"
        assert called_with["topic"] == "Python asyncio patterns"

    def test_research_stream_triggered(self, pipe):
        """Valid /research triggers header + _research_and_stream.

        §17.215 E2 — short query (2 tokens) needs ``--confirm`` to
        bypass the new disambiguation prompt.
        """
        stream_called = [False]

        def fake_stream(topic, depth):
            stream_called[0] = True
            yield "streaming..."

        pipe._research_and_stream = fake_stream
        output = list(pipe.pipe(
            user_message="/research Redis caching --confirm",
            model_id="test",
            messages=[{"role": "user", "content": "/research Redis caching --confirm"}],
            body={"messages": [{"role": "user", "content": "/research Redis caching --confirm"}]},
        ))
        combined = "".join(output)
        assert stream_called[0] is True
        assert "Researching" in combined
        assert "Redis caching" in combined

    def test_research_short_query_prompts_disambiguation(self, pipe):
        """§17.215 E2 — ``/research <short query>`` without --confirm
        surfaces the /rag vs /research disambiguation prompt instead of
        firing Phase 2. The prompt offers both copy-pasteable forms.
        """
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
        assert stream_called[0] is False
        assert "/rag Redis caching" in combined
        assert "/research Redis caching --confirm" in combined

    def test_research_long_topic_skips_disambiguation(self, pipe):
        """§17.215 E2 — topics with >4 tokens are unambiguous and fire
        Phase 2 directly without needing --confirm.
        """
        called_with = {}

        def fake_stream(topic, depth):
            called_with["topic"] = topic
            yield "streaming..."

        pipe._research_and_stream = fake_stream
        output = list(pipe.pipe(
            user_message="/research how does Redis handle persistence and replication",
            model_id="test",
            messages=[{"role": "user", "content": "/research how does Redis handle persistence and replication"}],
            body={"messages": [{"role": "user", "content": "/research how does Redis handle persistence and replication"}]},
        ))
        assert called_with.get("topic") == "how does Redis handle persistence and replication"

    def test_research_url_skips_disambiguation(self, pipe):
        """§17.215 E2 — URL inputs always pass through; the prefix
        regex matches https?:// regardless of token count.
        """
        called_with = {}

        def fake_stream(topic, depth):
            called_with["topic"] = topic
            yield "streaming..."

        pipe._research_and_stream = fake_stream
        url = "https://example.com/article"
        output = list(pipe.pipe(
            user_message=f"/research {url}",
            model_id="test",
            messages=[{"role": "user", "content": f"/research {url}"}],
            body={"messages": [{"role": "user", "content": f"/research {url}"}]},
        ))
        assert called_with.get("topic") == url

    def test_research_complete_suggests_go(self, pipe, monkeypatch):
        """After research_complete, output should prompt the user to type /go.

        X.18 — pre-X.18 patched ``_mod.requests.post`` but
        ``_research_and_stream_raw`` actually fires through
        ``_HTTP_SESSION.post`` (a Session, not the module-level
        ``requests.post``). The patch never intercepted the real call,
        so the test hit the live orchestrator and failed against
        whatever stale state that had. Patch the right target now.
        """
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
            _mod._HTTP_SESSION, "post", lambda *a, **kw: FakeResponse()
        )

        output = "".join(pipe._research_and_stream("Docker networking", "medium"))
        assert "/go" in output
        assert "build a project plan" in output
        assert "Research Complete" in output

    def test_awaiting_reply_renders_paused_block(self, pipe, monkeypatch):
        """awaiting_reply SSE event renders the pause block with session id + hints.

        X.18 — same patch-target fix as test_research_complete_suggests_go.
        """
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
            _mod._HTTP_SESSION, "post", lambda *a, **kw: FakeResponse()
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
        monkeypatch.setattr(_mod._HTTP_SESSION, "post", fake_post)

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
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
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
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            mg.return_value = MagicMock(status_code=404, text="")
            out = pipe._handle_results(["/results", "bogus"])
        assert "Job not found" in out
        assert "bogus" in out

    def test_running_shows_progress(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
            resp = MagicMock(status_code=200)
            # Round 6 (Apr 27 2026): /results derives progress from the
            # counts dict + nodes array (the orchestrator API contract).
            resp.json.return_value = {
                "job_status": "running",
                "total_nodes": 7,
                "counts": {"done": 3, "running": 1, "pending": 3},
                "nodes": [
                    {"node_key": "T1", "title": "step 1", "status": "done"},
                    {"node_key": "T2", "title": "step 2", "status": "done"},
                    {"node_key": "T3", "title": "step 3", "status": "done"},
                    {"node_key": "T4", "title": "write_report", "status": "running"},
                    {"node_key": "T5", "title": "step 5", "status": "pending"},
                    {"node_key": "T6", "title": "step 6", "status": "pending"},
                    {"node_key": "T7", "title": "step 7", "status": "pending"},
                ],
            }
            mg.return_value = resp
            out = pipe._handle_results(["/results", "j"])
        assert "running" in out
        assert "3/7" in out
        assert "T4" in out
        assert "write_report" in out

    def test_failed_shows_error(self, pipe):
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
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
        with patch("scaffold_router._HTTP_SESSION.get") as mg:
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
        with patch("scaffold_router._HTTP_SESSION.post", side_effect=_mp):
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
        with patch("scaffold_router._HTTP_SESSION.post", side_effect=_mp):
            pipe._handle_schedule('/schedule add "* * * * *" --depth medium my topic')
        assert captured["json"]["depth"] == "medium"

    def test_default_depth_medium(self, pipe):
        """When --depth is omitted, the pipeline now defaults to 'medium'.

        Round 6 (Apr 27 2026): aligned the pipeline default with
        ScheduleCreate.depth and scheduled_jobs.depth, both of which
        default to 'medium'. Previously was 'shallow' — three-way drift.
        """
        captured = {}
        def _mp(url, **kw):
            captured["json"] = kw.get("json")
            resp = MagicMock(status_code=200)
            resp.json.return_value = {"id": 3, "topic": "foo",
                                      "cron_expression": "0 0 * * *", "depth": "medium"}
            return resp
        with patch("scaffold_router._HTTP_SESSION.post", side_effect=_mp):
            pipe._handle_schedule('/schedule add "0 0 * * *" foo')
        assert captured["json"]["depth"] == "medium"

    def test_invalid_depth_value(self, pipe):
        out = pipe._handle_schedule('/schedule add "* * * * *" --depth=insane topic')
        assert "Invalid" in out or "insane" in out


# STEP 9: U.8.D — diagnostics + admin parity (/exec, /cleanup, /config, /logs, /health)
# ===================================================================


@pytest.mark.smoke
class TestU8DCommands:
    """Chat parity for components that previously only had CLI/SDK reach."""

    @patch("scaffold_router._HTTP_SESSION.post")
    def test_exec_retry(self, mock_post, pipe):
        mock_post.return_value = _make_response(200, {"status": "running"})
        out = pipe._handle_command("/exec retry abc-123 T2")
        mock_post.assert_called_once()
        # Endpoint + body
        url = mock_post.call_args[0][0]
        sent = mock_post.call_args[1].get("json", {})
        assert "/exec/retry" in url
        assert sent == {"job_id": "abc-123", "node_key": "T2"}

    def test_exec_help_when_no_subcommand(self, pipe):
        out = pipe._handle_command("/exec")
        assert "Usage" in out
        assert "/exec retry" in out

    def test_exec_retry_missing_args(self, pipe):
        out = pipe._handle_command("/exec retry abc-123")
        assert "Usage" in out

    @patch("scaffold_router._HTTP_SESSION.post")
    def test_exec_retry_rejects_placeholder(self, mock_post, pipe):
        out = pipe._handle_command("/exec retry <job_id> <node_key>")
        assert "placeholder" in out.lower()
        mock_post.assert_not_called()

    @patch("scaffold_router._HTTP_SESSION.post")
    def test_cleanup_renders_counts(self, mock_post, pipe):
        mock_post.return_value = _make_response(200, {
            "reaped_running_to_cancelled": 2,
            "reaped_orphans_reset": 1,
            "timestamp": "2026-05-07T18:00:00",  # non-int — should be filtered out
        })
        out = pipe._handle_command("/cleanup")
        url = mock_post.call_args[0][0]
        assert "/jobs/cleanup" in url
        assert "reaped_running_to_cancelled" in out
        assert "| 2 |" in out
        # The non-int timestamp should not appear as a count row
        assert "| 2026" not in out

    @patch("scaffold_router._HTTP_SESSION.get")
    def test_config_renders_table(self, mock_get, pipe):
        mock_get.return_value = _make_response(200, {"fields": [
            {"name": "log_level", "value": "INFO", "is_default": True},
            {"name": "model_general", "value": "qwen3:7b", "is_default": False},
        ], "count": 2})
        out = pipe._handle_command("/config")
        url = mock_get.call_args[0][0]
        assert "/config" in url
        assert "log_level" in out
        assert "model_general" in out

    @patch("scaffold_router._HTTP_SESSION.get")
    def test_config_substring_filter(self, mock_get, pipe):
        mock_get.return_value = _make_response(200, {"fields": [
            {"name": "log_level", "value": "INFO", "is_default": True},
            {"name": "model_general", "value": "qwen3:7b", "is_default": False},
            {"name": "model_coder", "value": "qwen2.5-coder", "is_default": False},
        ], "count": 3})
        out = pipe._handle_command("/config model")
        assert "model_general" in out and "model_coder" in out
        assert "log_level" not in out

    @patch("scaffold_router._HTTP_SESSION.get")
    def test_config_non_defaults_flag(self, mock_get, pipe):
        mock_get.return_value = _make_response(200, {"fields": [
            {"name": "log_level", "value": "INFO", "is_default": True},
            {"name": "model_general", "value": "qwen3:7b", "is_default": False},
        ], "count": 2})
        out = pipe._handle_command("/config --non-defaults")
        assert "model_general" in out
        assert "log_level" not in out

    @patch("scaffold_router._HTTP_SESSION.get")
    def test_logs_renders_node_table(self, mock_get, pipe):
        mock_get.return_value = _make_response(200, {
            "job_id": "abc-123",
            "job_status": "completed",
            "node_count": 2,
            "nodes": [
                {"node_key": "T1", "status": "done", "confidence": 0.92,
                 "tool": "LLM", "output_text": "Plan first"},
                {"node_key": "T2", "status": "done", "confidence": 0.85,
                 "tool": "LLM", "output_text": "Then build"},
            ],
        })
        out = pipe._handle_command("/logs abc-123")
        url = mock_get.call_args[0][0]
        assert "/logs/abc-123" in url
        assert "T1" in out and "T2" in out
        assert "Plan first" in out

    def test_logs_missing_job_id(self, pipe):
        out = pipe._handle_command("/logs")
        assert "Usage" in out

    @patch("scaffold_router._HTTP_SESSION.get")
    def test_logs_rejects_placeholder(self, mock_get, pipe):
        out = pipe._handle_command("/logs <job_id>")
        assert "placeholder" in out.lower()
        mock_get.assert_not_called()

    @patch("scaffold_router._HTTP_SESSION.get")
    def test_health_renders_subsystem_table(self, mock_get, pipe):
        mock_get.return_value = _make_response(200, {"checks": {
            "postgresql": {"status": "up", "latency_ms": 4},
            "ollama": {"status": "up", "latency_ms": 12},
            "milvus": {"status": "down", "latency_ms": 5000},
        }})
        out = pipe._handle_command("/health")
        url = mock_get.call_args[0][0]
        assert "/health" in url
        for name in ("postgresql", "ollama", "milvus"):
            assert name in out
        # Up icon + down icon should both render
        assert "✅" in out and "❌" in out

    def test_help_lists_new_commands(self, pipe):
        """The help text should advertise the new U.8.D commands."""
        out = pipe._handle_command("/help")
        for cmd in ("/health", "/logs", "/exec retry", "/cleanup", "/config"):
            assert cmd in out, f"expected `{cmd}` in /help output"

    def test_schedule_help_no_longer_advertises_run_now(self, pipe):
        """`run-now` was a vapor verb (no endpoint, no handler); removed in U.8.D."""
        # Subcommand catalog is the source of truth surfaced to autocompletion.
        from scaffold_router import KNOWN_SUBCOMMANDS  # type: ignore
        assert "run-now" not in KNOWN_SUBCOMMANDS["/schedule"]


# ===================================================================
# J.3.c — /cost <job_id> chat command
# ===================================================================


@pytest.mark.smoke
class TestCostCommand:
    """`/cost <job_id>` hits GET /jobs/{id}/costs and renders a totals
    header + per-(provider, model) breakdown table."""

    def test_usage_when_no_arg(self, pipe):
        out = pipe._handle_command("/cost")
        assert "Usage" in out
        assert "/cost" in out

    def test_rejects_placeholder(self, pipe):
        out = pipe._handle_command("/cost <job_id>")
        assert "placeholder" in out.lower()

    @patch("scaffold_router._HTTP_SESSION.get")
    def test_renders_breakdown_table(self, mock_get, pipe):
        mock_get.return_value = _make_response(200, {
            "job_id": "abc-123",
            "total_cost_usd": 0.0123,
            "total_prompt_tokens": 5000,
            "total_completion_tokens": 2000,
            "total_latency_ms": 30000,
            "call_count": 12,
            "by_provider": [
                {"provider": "openai", "model": "gpt-4o", "calls": 8,
                 "cost_usd": 0.012, "prompt_tokens": 4000,
                 "completion_tokens": 1500, "latency_ms": 22000},
                {"provider": "ollama", "model": "qwen3:4b", "calls": 4,
                 "cost_usd": 0.0003, "prompt_tokens": 1000,
                 "completion_tokens": 500, "latency_ms": 8000},
            ],
        })
        out = pipe._handle_command("/cost abc-123")
        url = mock_get.call_args[0][0]
        assert "/jobs/abc-123/costs" in url
        # Header + totals.
        assert "💰 Cost" in out
        assert "$0.0123" in out
        assert "12 calls" in out
        # Token + latency lines.
        assert "5,000" in out and "2,000" in out
        assert "30,000 ms" in out
        # Breakdown rows surface each (provider, model).
        assert "openai" in out and "gpt-4o" in out
        assert "ollama" in out and "qwen3:4b" in out

    @patch("scaffold_router._HTTP_SESSION.get")
    def test_zero_calls_renders_friendly_empty_state(self, mock_get, pipe):
        """A job with no logged LLM calls (or one that ran before the
        J.3.a migration) returns the zero shape from /jobs/{id}/costs.
        The OWUI render shows a friendly hint rather than an empty table."""
        mock_get.return_value = _make_response(200, {
            "job_id": "abc-123",
            "total_cost_usd": 0.0,
            "total_prompt_tokens": 0,
            "total_completion_tokens": 0,
            "total_latency_ms": 0,
            "call_count": 0,
            "by_provider": [],
        })
        out = pipe._handle_command("/cost abc-123")
        assert "💰 Cost" in out
        assert "0 calls" in out
        # Friendly empty-state message.
        assert "no LLM calls logged" in out

    @patch("scaffold_router._HTTP_SESSION.get")
    def test_http_error_propagates(self, mock_get, pipe):
        """4xx/5xx from the costs endpoint returns the standard _fmt
        error rendering rather than a half-built table."""
        mock_get.return_value = _make_response(500, {"detail": "boom"})
        out = pipe._handle_command("/cost abc-123")
        # _fmt's standard error envelope wraps the response.
        assert "500" in out or "boom" in out

    def test_cost_in_known_commands(self):
        """`/cost` is registered in KNOWN_COMMANDS so autocomplete + the
        unknown-command suggestion logic surface it."""
        from scaffold_router import KNOWN_COMMANDS  # type: ignore
        assert "/cost" in KNOWN_COMMANDS

    def test_help_advertises_cost(self, pipe):
        out = pipe._handle_command("/help")
        assert "/cost" in out


# ===================================================================
# Assist Mode chat-memory dispatch (per-chat session_id + node_key memory)
# ===================================================================


_UUID_A = "11111111-2222-3333-4444-555555555555"
_UUID_B = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def _http_call_log(monkeypatch):
    """Patch _HTTP_SESSION put/get/delete/post and return a call log.

    Each entry: (verb, url, json_body_or_None). Response is configured per
    (verb, path-prefix) via the returned `responses` dict.
    """
    log: list[tuple[str, str, dict | None]] = []
    responses: dict[tuple[str, str], MagicMock] = {}

    def _match(verb: str, url: str) -> MagicMock:
        for (v, prefix), resp in responses.items():
            if v == verb and prefix in url:
                return resp
        return _make_response(404, {"detail": "no mock"})

    def _put(url, **kw):
        log.append(("put", url, kw.get("json")))
        return _match("put", url)

    def _get(url, **kw):
        log.append(("get", url, None))
        return _match("get", url)

    def _delete(url, **kw):
        log.append(("delete", url, None))
        return _match("delete", url)

    def _post(url, **kw):
        log.append(("post", url, kw.get("json")))
        return _match("post", url)

    monkeypatch.setattr("scaffold_router._HTTP_SESSION.put", _put)
    monkeypatch.setattr("scaffold_router._HTTP_SESSION.get", _get)
    monkeypatch.setattr("scaffold_router._HTTP_SESSION.delete", _delete)
    monkeypatch.setattr("scaffold_router._HTTP_SESSION.post", _post)
    return log, responses


@pytest.mark.smoke
class TestAssistChatMemory:
    """`/assist <subcommand>` accepts an implicit session_id resolved via
    body['metadata']['chat_id'] -> /assist/_chatmap/{chat_id}."""

    def _body_with_chat(self, chat_id: str) -> dict:
        return {"metadata": {"chat_id": chat_id}}

    def test_start_remembers_session_in_chatmap(self, pipe, monkeypatch):
        """`/assist <job_id>` must PUT chat_id→session_id after starting."""
        log, responses = _http_call_log(monkeypatch)
        responses[("post", "/assist/start")] = _make_response(
            200, {"session_id": _UUID_A, "job_id": "job-1", "pending_steps": 3},
        )
        responses[("get", f"/assist/{_UUID_A}/next")] = _make_response(
            200, {"session_id": _UUID_A, "node_key": "T1", "title": "step",
                  "status": "active", "depends_on": []},
        )
        responses[("put", "/assist/_chatmap/")] = _make_response(200, {"stored": True})

        list(pipe.pipe("/assist job-1", "m", [], self._body_with_chat("chat-A")))
        puts = [e for e in log if e[0] == "put" and "_chatmap/chat-A" in e[1]]
        assert puts, f"expected PUT to /assist/_chatmap/chat-A, got: {log}"
        assert puts[0][2] == {"session_id": _UUID_A, "last_node_key": "T1"} or \
               puts[0][2] == {"session_id": _UUID_A, "last_node_key": None}

    def test_next_resolves_session_from_chatmap_when_arg_omitted(self, pipe, monkeypatch):
        """`/assist next` with no session_id falls back to GET _chatmap."""
        log, responses = _http_call_log(monkeypatch)
        responses[("get", "/assist/_chatmap/chat-B")] = _make_response(
            200, {"chat_id": "chat-B", "session_id": _UUID_A, "last_node_key": None},
        )
        responses[("get", f"/assist/{_UUID_A}/next")] = _make_response(
            200, {"session_id": _UUID_A, "node_key": "T1", "title": "step",
                  "status": "active", "depends_on": []},
        )
        responses[("put", "/assist/_chatmap/")] = _make_response(200, {"stored": True})

        out = "".join(pipe.pipe("/assist next", "m", [], self._body_with_chat("chat-B")))
        # The /next call must have hit the recalled session id.
        next_calls = [e for e in log if e[0] == "get" and f"/assist/{_UUID_A}/next" in e[1]]
        assert next_calls, f"expected GET /assist/{_UUID_A}/next, got: {log}"
        assert "T1" in out

    def test_explicit_uuid_arg_overrides_chatmap(self, pipe, monkeypatch):
        """An explicit UUID first arg must beat the recalled session — the
        whole reason chat memory is opt-in: the user is steering."""
        log, responses = _http_call_log(monkeypatch)
        # Chatmap says UUID_A, but user types UUID_B.
        responses[("get", "/assist/_chatmap/chat-C")] = _make_response(
            200, {"chat_id": "chat-C", "session_id": _UUID_A, "last_node_key": None},
        )
        responses[("get", f"/assist/{_UUID_B}/next")] = _make_response(
            200, {"session_id": _UUID_B, "node_key": "T9", "title": "x",
                  "status": "active", "depends_on": []},
        )
        responses[("put", "/assist/_chatmap/")] = _make_response(200, {"stored": True})

        list(pipe.pipe(f"/assist next {_UUID_B}", "m", [], self._body_with_chat("chat-C")))
        next_calls = [e for e in log if e[0] == "get" and "/next" in e[1]]
        assert any(_UUID_B in url for _, url, _ in next_calls), \
            f"explicit UUID must win; got: {next_calls}"
        assert not any(_UUID_A in url for _, url, _ in next_calls), \
            f"explicit UUID must override recall; got: {next_calls}"

    def test_missing_chat_id_and_no_arg_yields_friendly_error(self, pipe, monkeypatch):
        """No chat_id (e.g. CLI/curl) and no explicit session_id should
        produce an actionable usage hint, not a NoneType crash."""
        _http_call_log(monkeypatch)
        out = "".join(pipe.pipe("/assist next", "m", [], {}))
        assert "No active assist session" in out
        assert "/assist <job_id>" in out

    def test_submit_renders_must_claim_first_hint(self, pipe, monkeypatch):
        """409 with error_code=must_claim_first → actionable hint, not raw HTTP."""
        log, responses = _http_call_log(monkeypatch)
        responses[("get", "/assist/_chatmap/chat-D")] = _make_response(
            200, {"chat_id": "chat-D", "session_id": _UUID_A, "last_node_key": "T1"},
        )
        responses[("post", f"/assist/{_UUID_A}/submit")] = _make_response(
            409, {"detail": {"error_code": "must_claim_first",
                             "message": "step T1 is pending"}},
        )

        msg = f"/assist submit\n```\nsome evidence\n```"
        out = "".join(pipe.pipe(msg, "m", [], self._body_with_chat("chat-D")))
        assert "claim it first" in out.lower() or "claim" in out.lower()
        assert "/assist next" in out

    def test_done_clears_chatmap_on_terminal_session(self, pipe, monkeypatch):
        """`/assist done` on a completed/abandoned/cancelled session must
        DELETE the chatmap so the next `/assist <job_id>` starts clean."""
        log, responses = _http_call_log(monkeypatch)
        responses[("get", "/assist/_chatmap/chat-E")] = _make_response(
            200, {"chat_id": "chat-E", "session_id": _UUID_A, "last_node_key": None},
        )
        responses[("get", f"/assist/{_UUID_A}")] = _make_response(
            200, {"session_id": _UUID_A, "job_id": "job-1", "status": "completed"},
        )
        responses[("get", "/exec/status/")] = _make_response(
            200, {"status": "completed", "compiled_output": "ok"},
        )
        responses[("delete", "/assist/_chatmap/")] = _make_response(200, {"cleared": True})

        list(pipe.pipe("/assist done", "m", [], self._body_with_chat("chat-E")))
        deletes = [e for e in log if e[0] == "delete" and "_chatmap/chat-E" in e[1]]
        assert deletes, f"expected DELETE /assist/_chatmap/chat-E on terminal status; got: {log}"

    def test_submit_uses_remembered_node_key(self, pipe, monkeypatch):
        """`/assist submit` with no node_key falls back to last_node_key
        from chat memory."""
        log, responses = _http_call_log(monkeypatch)
        responses[("get", "/assist/_chatmap/chat-F")] = _make_response(
            200, {"chat_id": "chat-F", "session_id": _UUID_A, "last_node_key": "T7"},
        )
        responses[("post", f"/assist/{_UUID_A}/submit")] = _make_response(
            200, {"node_key": "T7", "status": "committed", "next_node_key": None},
        )
        responses[("put", "/assist/_chatmap/")] = _make_response(200, {"stored": True})

        msg = "/assist submit\n```\nevidence here\n```"
        list(pipe.pipe(msg, "m", [], self._body_with_chat("chat-F")))
        posts = [e for e in log if e[0] == "post" and "/submit" in e[1]]
        assert posts and posts[0][2]["node_key"] == "T7", \
            f"expected node_key='T7' from chat memory; got: {posts}"

    def test_start_handles_non_json_body(self, pipe, monkeypatch):
        """§17.259 — orchestrator returns HTTP 200 with non-JSON body.

        Pre-fix, `r.json()` raised ValueError mid-generator, crashing the
        chat thread with no recovery. Post-fix, yields an `❌` line and
        returns cleanly."""
        _log, responses = _http_call_log(monkeypatch)
        responses[("post", "/assist/start")] = _make_response(200, "not json at all")

        out = "".join(pipe.pipe("/assist job-1", "m", [], self._body_with_chat("chat-G")))
        assert "❌" in out and "non-JSON" in out, \
            f"expected JSON-parse error yield; got: {out!r}"

    def test_start_handles_missing_session_id(self, pipe, monkeypatch):
        """§17.259 — orchestrator returns HTTP 200 + valid JSON, no session_id.

        Pre-fix, `d['session_id']` raised KeyError mid-generator. Post-fix,
        yields an `❌` line and returns cleanly."""
        _log, responses = _http_call_log(monkeypatch)
        responses[("post", "/assist/start")] = _make_response(
            200, {"job_id": "job-1", "pending_steps": 3},  # no session_id
        )

        out = "".join(pipe.pipe("/assist job-1", "m", [], self._body_with_chat("chat-H")))
        assert "❌" in out and "session_id" in out, \
            f"expected missing-session_id error yield; got: {out!r}"

    def test_start_tolerates_missing_optional_fields(self, pipe, monkeypatch):
        """§17.259 — orchestrator returns session_id but no job_id/pending_steps.

        These are display-only fields; session_id is the only load-bearing
        key. Pre-fix, KeyError on `d['job_id']`. Post-fix, falls back to
        the input job_id and renders `?` for unknown pending count."""
        _log, responses = _http_call_log(monkeypatch)
        responses[("post", "/assist/start")] = _make_response(
            200, {"session_id": _UUID_A},  # no job_id, no pending_steps
        )
        responses[("get", f"/assist/{_UUID_A}/next")] = _make_response(
            200, {"session_id": _UUID_A, "node_key": "T1", "title": "step",
                  "status": "active", "depends_on": []},
        )
        responses[("put", "/assist/_chatmap/")] = _make_response(200, {"stored": True})

        out = "".join(pipe.pipe("/assist job-99", "m", [], self._body_with_chat("chat-I")))
        # Session-started banner should still render, falling back to input job_id
        # and showing "?" for the unknown pending-steps count.
        assert "job-99" in out, f"expected input job_id fallback; got: {out!r}"
        assert "? pending" in out, f"expected '?' for unknown pending count; got: {out!r}"

