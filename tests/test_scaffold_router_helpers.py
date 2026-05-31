"""Tests for scaffold_router.py — pure helper functions and low-level glue.

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


@pytest.mark.smoke
class TestNoiseInputGuard:
    """§17.349 — bare single-char input ("a", "?", etc.) skips the LLM
    call. Closes the operator-reported bug from the §17.342 transcript
    where a bare "a" triggered an expensive triage roundtrip that
    treated the noise as a real input.
    """

    def test_bare_single_char_after_first_turn_skips_triage(self, pipe):
        """Mid-conversation single-char input → friendly nudge, no LLM call."""
        messages = [
            {"role": "user", "content": "Build a homelab dashboard"},
            {"role": "assistant", "content": "Sure, what's the use case?"},
            {"role": "user", "content": "a"},  # noise
        ]
        with patch.object(pipe, "_call_triage") as mock_triage:
            chunks = list(pipe.pipe("a", "test-model", messages, {}))
        combined = "".join(chunks)
        assert "didn't catch that" in combined
        mock_triage.assert_not_called()

    def test_bare_question_mark_skips_triage(self, pipe):
        """Single '?' is also noise; should skip triage."""
        messages = [
            {"role": "user", "content": "real prior message"},
            {"role": "assistant", "content": "reply"},
            {"role": "user", "content": "?"},
        ]
        with patch.object(pipe, "_call_triage") as mock_triage:
            chunks = list(pipe.pipe("?", "test-model", messages, {}))
        combined = "".join(chunks)
        assert "didn't catch that" in combined
        mock_triage.assert_not_called()

    def test_short_real_word_invokes_triage(self, pipe):
        """'ok' is terse but a real intent signal — must NOT be filtered."""
        messages = [
            {"role": "user", "content": "Build a homelab dashboard"},
            {"role": "assistant", "content": "Sure, what's the use case?"},
            {"role": "user", "content": "ok"},
        ]
        with patch.object(pipe, "_call_triage", return_value="triage response") as mock_triage:
            list(pipe.pipe("ok", "test-model", messages, {}))
        mock_triage.assert_called_once()

    def test_first_turn_single_char_still_invokes_triage(self, pipe):
        """First-turn input is exempt — the welcome preamble handles
        orientation, no second-guess on top."""
        messages = [{"role": "user", "content": "a"}]
        with patch.object(pipe, "_call_triage", return_value="triage response") as mock_triage:
            list(pipe.pipe("a", "test-model", messages, {}))
        mock_triage.assert_called_once()


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
        """Help text should mention every user-facing command.

        ``/dag`` is intentionally omitted as of 2026-05-03 — see
        ``references/commands.md`` ("internal/scripted-callers-only").
        It used to be asserted here and accounted for one of the
        pre-existing baseline failures until U.8.G dropped it.
        """
        result = pipe._help()
        for cmd in ["/go", "/idea", "/execute", "/confirm",
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
        """A 'blocked' event means the pipeline can't make progress.

        §17.295 — the terminal `blocked` SSE event carries a
        `blocked_nodes` list with per-node `cause` ("failed" or "waiting")
        and per-dep `{node_key, status}` objects. Pre-§17.295 this test
        mocked a different (single-node, top-level `blocked_by`) shape
        that didn't reflect what execute_all_nodes actually emits — the
        handler tolerated it accidentally. §17.295's rewrite matched the
        real payload, surfacing the test's wrong mock.
        """
        data = json.dumps({
            "message": "No executable nodes — 1 blocked by failed upstream.",
            "blocked_nodes": [
                {
                    "node_key": "T3",
                    "title": "Summarize",
                    "blocked_by": [
                        {"node_key": "T1", "status": "failed"},
                        {"node_key": "T2", "status": "pending"},
                    ],
                    "cause": "failed",
                },
            ],
            "actionable_count": 1,
            "waiting_count": 0,
        })
        failed = []
        chunks = list(pipe._handle_sse_event("blocked", data, failed))
        # Generator yields multiple chunks (header + per-node bullets).
        joined = "".join(chunks)
        assert "T3" in joined
        # Failed-upstream dep surfaced with the /exec retry hint.
        assert "T1" in joined
        assert "/exec retry" in joined

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
class TestWindowMessages:
    """_window_messages: caps triage history to last N turns; pins first user message."""

    def test_returns_input_when_under_window(self, pipe):
        """Conversation shorter than window: pass through unchanged."""
        msgs = [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
        ]
        pipe.valves.triage_history_window = 8
        assert pipe._window_messages(msgs) == msgs

    def test_pins_first_user_when_outside_window(self, pipe):
        """First user message is preserved when older than the tail window."""
        msgs = [{"role": "user", "content": "seed"}]
        msgs += [{"role": "assistant", "content": f"a{i}"} for i in range(5)]
        msgs += [{"role": "user", "content": f"u{i}"} for i in range(5)]
        pipe.valves.triage_history_window = 4
        out = pipe._window_messages(msgs)
        assert out[0] == {"role": "user", "content": "seed"}
        assert len(out) == 5  # 1 pinned + 4 tail
        assert out[-4:] == msgs[-4:]

    def test_no_duplicate_when_first_user_in_tail(self, pipe):
        """If the first user message already lies inside the tail, don't duplicate it."""
        msgs = [
            {"role": "user", "content": "u1"},
            {"role": "assistant", "content": "a1"},
            {"role": "user", "content": "u2"},
        ]
        pipe.valves.triage_history_window = 8
        out = pipe._window_messages(msgs)
        assert out == msgs  # under-window passthrough; no dup

    def test_returns_tail_only_when_no_user_message(self, pipe):
        """Edge: assistant-only messages → just return last N."""
        msgs = [{"role": "assistant", "content": f"a{i}"} for i in range(10)]
        pipe.valves.triage_history_window = 3
        out = pipe._window_messages(msgs)
        assert out == msgs[-3:]

    def test_window_size_one_floor(self, pipe):
        """A window value of 0 or negative is clamped to 1; first user still pinned."""
        msgs = [{"role": "user", "content": f"u{i}"} for i in range(5)]
        pipe.valves.triage_history_window = 0
        out = pipe._window_messages(msgs)
        # n clamped to 1; first user (u0) outside tail -> pinned + tail (u4)
        assert out == [msgs[0], msgs[-1]]

    def test_preserves_message_order(self, pipe):
        """Pinned-then-tail must remain chronological."""
        msgs = [{"role": "user", "content": "seed"}]
        msgs += [{"role": "user", "content": f"u{i}"} for i in range(10)]
        pipe.valves.triage_history_window = 3
        out = pipe._window_messages(msgs)
        assert out[0]["content"] == "seed"
        assert [m["content"] for m in out[1:]] == ["u7", "u8", "u9"]

@pytest.mark.smoke
class TestLogPipeInputs:
    """_log_pipe_inputs: diagnostic logger for OWUI file-routing failures."""

    def test_logs_when_called(self, pipe, capsys):
        """Helper writes a PIPE_INPUTS line to stdout when invoked."""
        pipe._log_pipe_inputs("hello", [{"role": "user", "content": "hi"}], {"k": 1})
        out = capsys.readouterr().out
        assert "PIPE_INPUTS" in out
        assert "user_message_len=5" in out
        assert "messages_n=1" in out

    def test_captures_body_keys_and_files(self, pipe, capsys):
        """Body keys and files_count surface in the log line."""
        body = {"files": [{"id": "a"}, {"id": "b"}], "stream": True}
        pipe._log_pipe_inputs("x", [], body)
        out = capsys.readouterr().out
        assert "files_count=2" in out
        assert "'files'" in out and "'stream'" in out

    def test_captures_metadata_files(self, pipe, capsys):
        """Falls back to metadata.files when top-level files missing."""
        body = {"metadata": {"files": [{"id": "a"}]}}
        pipe._log_pipe_inputs("x", [], body)
        out = capsys.readouterr().out
        assert "files_count=1" in out
        assert "metadata_keys=['files']" in out

    def test_handles_non_dict_body(self, pipe, capsys):
        """Non-dict body is tolerated — no exception, sane defaults."""
        pipe._log_pipe_inputs("x", [], None)
        out = capsys.readouterr().out
        assert "PIPE_INPUTS" in out
        assert "files_count=0" in out

    def test_disabled_by_default(self, pipe, capsys):
        """Valve is False by default; pipe() must not call the logger."""
        assert pipe.valves.log_pipe_inputs is False

@pytest.mark.smoke
class TestEnvFallbacks:
    """_apply_env_fallbacks: empty valves recover from SCAFFOLD_* env vars."""

    def test_empty_api_key_loads_from_env(self, pipe, monkeypatch):
        """Empty api_key valve loads SCAFFOLD_API_KEY from env."""
        monkeypatch.setenv("SCAFFOLD_API_KEY", "sk-test-from-env")
        pipe.valves.api_key = ""
        pipe._apply_env_fallbacks()
        assert pipe.valves.api_key == "sk-test-from-env"

    def test_nonempty_valve_not_overwritten(self, pipe, monkeypatch):
        """If valve is already set, env is not consulted."""
        monkeypatch.setenv("SCAFFOLD_API_KEY", "sk-from-env")
        pipe.valves.api_key = "sk-from-valve"
        pipe._apply_env_fallbacks()
        assert pipe.valves.api_key == "sk-from-valve"

    def test_missing_env_leaves_empty(self, pipe, monkeypatch):
        """If valve empty AND env missing, value stays empty (no crash)."""
        monkeypatch.delenv("SCAFFOLD_API_KEY", raising=False)
        pipe.valves.api_key = ""
        pipe._apply_env_fallbacks()
        assert pipe.valves.api_key == ""

    def test_auth_headers_falls_back_to_env(self, pipe, monkeypatch):
        """_auth_headers reads env when valve empty (belt + braces)."""
        monkeypatch.setenv("SCAFFOLD_API_KEY", "sk-fallback")
        pipe.valves.api_key = ""
        assert pipe._auth_headers() == {"X-API-Key": "sk-fallback"}

    def test_orchestrator_url_fallback(self, pipe, monkeypatch):
        """Other env-mapped valves also fall back."""
        monkeypatch.setenv("SCAFFOLD_ORCHESTRATOR_URL", "http://test:8000")
        pipe.valves.orchestrator_url = ""
        pipe._apply_env_fallbacks()
        assert pipe.valves.orchestrator_url == "http://test:8000"


@pytest.mark.smoke
class TestEnvOverridePrecedence:
    """SCAFFOLD_VALVES_ENV_OVERRIDE: when true, env beats non-empty valve."""

    def test_override_true_env_beats_valve(self, pipe, monkeypatch):
        monkeypatch.setenv("SCAFFOLD_VALVES_ENV_OVERRIDE", "true")
        monkeypatch.setenv("SCAFFOLD_API_KEY", "sk-from-env")
        pipe.valves.api_key = "sk-stale-from-valve"
        pipe._apply_env_fallbacks()
        assert pipe.valves.api_key == "sk-from-env"

    def test_override_unset_keeps_valve(self, pipe, monkeypatch):
        """Default (override unset): non-empty valve wins. Backward-compat guard."""
        monkeypatch.delenv("SCAFFOLD_VALVES_ENV_OVERRIDE", raising=False)
        monkeypatch.setenv("SCAFFOLD_API_KEY", "sk-from-env")
        pipe.valves.api_key = "sk-from-valve"
        pipe._apply_env_fallbacks()
        assert pipe.valves.api_key == "sk-from-valve"

    def test_override_false_keeps_valve(self, pipe, monkeypatch):
        monkeypatch.setenv("SCAFFOLD_VALVES_ENV_OVERRIDE", "false")
        monkeypatch.setenv("SCAFFOLD_API_KEY", "sk-from-env")
        pipe.valves.api_key = "sk-from-valve"
        pipe._apply_env_fallbacks()
        assert pipe.valves.api_key == "sk-from-valve"

    def test_override_url_too(self, pipe, monkeypatch):
        """Override mode covers all managed string valves, not just api_key."""
        monkeypatch.setenv("SCAFFOLD_VALVES_ENV_OVERRIDE", "1")
        monkeypatch.setenv("SCAFFOLD_ORCHESTRATOR_URL", "http://prod:8000")
        pipe.valves.orchestrator_url = "http://stale:8000"
        pipe._apply_env_fallbacks()
        assert pipe.valves.orchestrator_url == "http://prod:8000"

    def test_override_with_empty_env_keeps_valve(self, pipe, monkeypatch):
        """Override only fires when env_val is non-empty; empty env != force-clear."""
        monkeypatch.setenv("SCAFFOLD_VALVES_ENV_OVERRIDE", "true")
        monkeypatch.delenv("SCAFFOLD_API_KEY", raising=False)
        pipe.valves.api_key = "sk-from-valve"
        pipe._apply_env_fallbacks()
        assert pipe.valves.api_key == "sk-from-valve"


@pytest.mark.smoke
class TestBootstrapValves:
    """_bootstrap_valves_from_template: seed empty valves.json from template."""

    def test_seeds_empty_live_file(self, pipe, tmp_path, monkeypatch):
        """Empty {} live file gets replaced with template content."""
        sub = tmp_path / "scaffold_router"
        sub.mkdir()
        tmpl = sub / "valves.template.json"
        tmpl.write_text('{"api_key":"","triage_model":"qwen3:4b"}')
        live = sub / "valves.json"
        live.write_text("{}")
        monkeypatch.setattr(
            "os.path.dirname",
            lambda _: str(tmp_path),
        )
        pipe._bootstrap_valves_from_template()
        assert "qwen3:4b" in live.read_text()

    def test_raises_when_template_missing(self, pipe, tmp_path, monkeypatch):
        """Missing template = RuntimeError (fail closed).

        Bootstrap must refuse to start the pipeline when the template is
        absent — silent no-op hides volume-mount misconfigurations and
        leaves the pipeline running with whatever stale state happened to
        be on disk. See HIGH #5 in the May 2026 review.
        """
        import pytest
        sub = tmp_path / "scaffold_router"
        sub.mkdir()
        live = sub / "valves.json"
        live.write_text("{}")
        monkeypatch.setattr("os.path.dirname", lambda _: str(tmp_path))
        with pytest.raises(RuntimeError, match="valves.template.json missing"):
            pipe._bootstrap_valves_from_template()

    def test_skips_when_live_has_real_content(self, pipe, tmp_path, monkeypatch):
        """Live file with real values is left alone."""
        sub = tmp_path / "scaffold_router"
        sub.mkdir()
        (sub / "valves.template.json").write_text('{"api_key":"FROM_TMPL"}')
        live = sub / "valves.json"
        live.write_text('{"api_key":"FROM_USER"}')
        monkeypatch.setattr("os.path.dirname", lambda _: str(tmp_path))
        pipe._bootstrap_valves_from_template()
        assert "FROM_USER" in live.read_text()
        assert "FROM_TMPL" not in live.read_text()


@pytest.mark.smoke
class TestPostWithKeepaliveProgressMarkers:
    """§17.173 — _post_with_keepalive emits visible elapsed-time markers
    when a progress_label is supplied. Without a label (or with
    progress_marker_interval=0), behavior is unchanged: only zero-width
    keepalive ticks are emitted between the POST start and return.

    The visible-marker-fires path needs a slow POST + controlled clock to
    test deterministically; we cover that path via the live OWUI flow.
    What we lock here is (a) the new valve default, (b) the keyword-only
    kwarg shape, and (c) the back-compat invariants (no label or
    interval=0 → behavior unchanged from pre-§17.173).
    """

    def test_progress_marker_interval_valve_default(self, pipe):
        """New valve exists with default 120 (~2 min between visible
        markers — chosen so a 25-min Phase 2 yields ~10 markers, not
        50+ chat-cluttering lines)."""
        assert pipe.valves.progress_marker_interval == 120

    def test_post_with_keepalive_accepts_progress_label_kwarg(self, pipe):
        """The new kwarg must be keyword-only (callers can't accidentally
        pass it positionally and bypass the in-between zero-width tick
        behavior)."""
        import inspect
        sig = inspect.signature(pipe._post_with_keepalive)
        param = sig.parameters.get("progress_label")
        assert param is not None
        assert param.kind == inspect.Parameter.KEYWORD_ONLY
        assert param.default is None

    def test_no_visible_marker_when_label_absent(self, pipe):
        """Back-compat: without progress_label, the generator only yields
        zero-width spaces — no '⏳' lines anywhere. Pre-§17.173 callers
        keep their previous behavior."""
        fake_resp = MagicMock(status_code=200)
        with patch.object(_mod, "_HTTP_SESSION") as sess:
            sess.post = MagicMock(return_value=fake_resp)
            chunks = list(pipe._post_with_keepalive("http://x", {}, 5))
        assert all("⏳" not in c for c in chunks)

    def test_no_visible_marker_when_interval_zero(self, pipe):
        """Operator escape hatch: setting progress_marker_interval=0
        disables visible markers even with progress_label set. Lets
        operators silence the markers without code changes."""
        pipe.valves.progress_marker_interval = 0
        fake_resp = MagicMock(status_code=200)
        with patch.object(_mod, "_HTTP_SESSION") as sess:
            sess.post = MagicMock(return_value=fake_resp)
            chunks = list(pipe._post_with_keepalive(
                "http://x", {}, 5, progress_label="Test phase",
            ))
        assert all("⏳" not in c for c in chunks)

