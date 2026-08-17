"""§17.786 — LLM trace-capture tests.

Coverage:
  - _truncate: None passthrough, within-limit unchanged, over-limit marked.
  - _serialize_request: messages → JSON, prompt → str, empty snapshot → None.
  - _serialize_tool_calls: None when no calls; JSON list when present.
  - set_current_request: no-op when the valve is off; sets a snapshot when on.
  - record_trace: no-op (no DB touch) when the valve is off; when on writes one
    llm_traces row with the right shape, reads job/node/call_kind ContextVars,
    applies truncation, serializes tool calls, and swallows DB failures so trace
    capture can never break the LLM call path.
  - model_router wiring: _begin_trace stashes the request snapshot that
    record_trace later reads (round-trip through the ContextVar).
"""
from __future__ import annotations

from contextvars import copy_context
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.config import settings
from app.utils.cost_tracking import (
    current_call_kind,
    current_job_id,
    current_node_id,
)
from app.utils.trace_capture import (
    _serialize_request,
    _serialize_tool_calls,
    _truncate,
    current_request,
    record_trace,
    set_current_request,
)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestTruncate:
    def test_none_passthrough(self):
        assert _truncate(None, 100) is None

    def test_within_limit_unchanged(self):
        assert _truncate("hello", 100) == "hello"

    def test_over_limit_marked(self):
        out = _truncate("abcdefghij", 4)
        assert out == "abcd…[+6 chars]"

    def test_limit_clamped_to_one(self):
        # A pathological limit <= 0 must not raise / slice weirdly.
        out = _truncate("abc", 0)
        assert out.startswith("a")
        assert "[+2 chars]" in out


@pytest.mark.smoke
class TestSerializeRequest:
    def test_none_snapshot(self):
        assert _serialize_request(None) is None

    def test_messages_serialized_as_json(self):
        snap = {"messages": [{"role": "user", "content": "hi"}], "prompt": None}
        out = _serialize_request(snap)
        assert '"role"' in out and '"user"' in out and '"hi"' in out

    def test_prompt_returned_as_string(self):
        assert _serialize_request({"prompt": "just text"}) == "just text"

    def test_messages_take_precedence_over_prompt(self):
        snap = {"messages": [{"role": "user", "content": "m"}], "prompt": "p"}
        assert '"m"' in _serialize_request(snap)

    def test_empty_snapshot_returns_none(self):
        assert _serialize_request({}) is None


@pytest.mark.smoke
class TestSerializeToolCalls:
    def test_no_tool_calls_returns_none(self):
        assert _serialize_tool_calls(SimpleNamespace(tool_calls=[])) is None
        assert _serialize_tool_calls(SimpleNamespace()) is None

    def test_tool_calls_serialized(self):
        tc = SimpleNamespace(id="coaxed_0", name="pick", arguments={"x": 1})
        out = _serialize_tool_calls(SimpleNamespace(tool_calls=[tc]))
        assert '"name": "pick"' in out
        assert '"coaxed_0"' in out
        assert '"x": 1' in out


# ---------------------------------------------------------------------------
# set_current_request — valve-gated request snapshot
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestSetCurrentRequest:
    def test_noop_when_valve_off(self, monkeypatch):
        monkeypatch.setattr(settings, "trace_capture_enabled", False, raising=False)
        ctx = copy_context()

        def _drive():
            set_current_request("generate", prompt="p")
            return current_request.get()

        assert ctx.run(_drive) is None

    def test_sets_snapshot_when_valve_on(self, monkeypatch):
        monkeypatch.setattr(settings, "trace_capture_enabled", True, raising=False)
        ctx = copy_context()

        def _drive():
            set_current_request(
                "generate", prompt="p", system="s",
                temperature=0.3, max_tokens=512,
            )
            return current_request.get()

        snap = ctx.run(_drive)
        assert snap["kind"] == "generate"
        assert snap["prompt"] == "p"
        assert snap["system"] == "s"
        assert snap["temperature"] == 0.3
        assert snap["max_tokens"] == 512


# ---------------------------------------------------------------------------
# record_trace — the DB writer
# ---------------------------------------------------------------------------


def _capturing_db(captured: dict):
    """An async-context-manager DB whose INSERT captures its params."""

    class _FakeDB:
        async def execute(self, sql, params=None):
            if "INSERT INTO llm_traces" in str(sql):
                captured.update(params or {})
            return MagicMock()

        async def commit(self):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    return _FakeDB


@pytest.mark.smoke
class TestRecordTrace:
    async def test_noop_when_valve_off(self, monkeypatch):
        monkeypatch.setattr(settings, "trace_capture_enabled", False, raising=False)
        captured: dict = {}

        # If it were to run, this DB would record the INSERT. It must not be
        # touched at all when the valve is off.
        with patch("app.database.async_session", _capturing_db(captured)):
            resp = SimpleNamespace(
                provider="ollama", model="qwen3:4b", text="hi",
                tokens_prompt=1, tokens_completion=1, total_duration_ms=1,
                success=True, error=None, tool_calls=[],
            )
            await record_trace(resp)

        assert captured == {}

    async def test_writes_row_with_context_and_content(self, monkeypatch):
        monkeypatch.setattr(settings, "trace_capture_enabled", True, raising=False)
        monkeypatch.setattr(settings, "trace_capture_max_chars", 8000, raising=False)
        captured: dict = {}
        ctx = copy_context()

        async def _runner():
            current_job_id.set("job-abc")
            current_node_id.set("node-xyz")
            current_call_kind.set("synthesis")
            set_current_request(
                "chat", messages=[{"role": "user", "content": "ping"}],
                system="be terse", temperature=0.2, max_tokens=256,
            )
            resp = SimpleNamespace(
                provider="ollama", model="qwen3:4b", text="pong",
                tokens_prompt=10, tokens_completion=4, total_duration_ms=99,
                success=True, error=None, tool_calls=[],
            )
            with patch("app.database.async_session", _capturing_db(captured)):
                await record_trace(resp)

        await ctx.run(_runner)

        assert captured["job_id"] == "job-abc"
        assert captured["node_id"] == "node-xyz"
        assert captured["call_kind"] == "synthesis"
        assert captured["request_kind"] == "chat"
        assert captured["provider"] == "ollama"
        assert captured["model"] == "qwen3:4b"
        assert captured["system_prompt"] == "be terse"
        assert '"ping"' in captured["request_content"]
        assert captured["response_content"] == "pong"
        assert captured["temperature"] == 0.2
        assert captured["max_tokens"] == 256
        assert captured["prompt_tokens"] == 10
        assert captured["completion_tokens"] == 4
        assert captured["latency_ms"] == 99
        assert captured["success"] is True
        assert captured["error"] is None
        assert captured["tool_calls"] is None

    async def test_truncates_oversized_content(self, monkeypatch):
        monkeypatch.setattr(settings, "trace_capture_enabled", True, raising=False)
        monkeypatch.setattr(settings, "trace_capture_max_chars", 10, raising=False)
        captured: dict = {}
        ctx = copy_context()

        async def _runner():
            set_current_request("generate", prompt="x" * 100)
            resp = SimpleNamespace(
                provider="ollama", model="m", text="y" * 100,
                tokens_prompt=0, tokens_completion=0, total_duration_ms=0,
                success=True, error=None, tool_calls=[],
            )
            with patch("app.database.async_session", _capturing_db(captured)):
                await record_trace(resp)

        await ctx.run(_runner)
        assert captured["request_content"] == "x" * 10 + "…[+90 chars]"
        assert captured["response_content"] == "y" * 10 + "…[+90 chars]"

    async def test_serializes_tool_calls(self, monkeypatch):
        monkeypatch.setattr(settings, "trace_capture_enabled", True, raising=False)
        captured: dict = {}
        ctx = copy_context()

        async def _runner():
            set_current_request("tool_call", messages=[{"role": "user", "content": "q"}])
            tc = SimpleNamespace(id="coaxed_0", name="choose", arguments={"a": 1})
            resp = SimpleNamespace(
                provider="ollama", model="m", text="",
                tokens_prompt=0, tokens_completion=0, total_duration_ms=0,
                success=True, error=None, tool_calls=[tc],
            )
            with patch("app.database.async_session", _capturing_db(captured)):
                await record_trace(resp)

        await ctx.run(_runner)
        assert '"name": "choose"' in captured["tool_calls"]

    async def test_no_context_vars_writes_nulls(self, monkeypatch):
        monkeypatch.setattr(settings, "trace_capture_enabled", True, raising=False)
        captured: dict = {}
        ctx = copy_context()

        async def _runner():
            # No set_current_request, no job/node ContextVars — an off-router
            # caller still records a row, just ungrouped and content-free.
            resp = SimpleNamespace(
                provider="", model="", text=None,
                tokens_prompt=None, tokens_completion=None, total_duration_ms=0,
                success=False, error="boom", tool_calls=[],
            )
            with patch("app.database.async_session", _capturing_db(captured)):
                await record_trace(resp)

        await ctx.run(_runner)
        assert captured["job_id"] is None
        assert captured["node_id"] is None
        assert captured["request_kind"] == "unknown"
        assert captured["provider"] == "unknown"
        assert captured["model"] == "unknown"
        assert captured["request_content"] is None
        assert captured["response_content"] is None
        assert captured["success"] is False
        assert captured["error"] == "boom"

    async def test_db_failure_swallowed(self, monkeypatch):
        """A DB write that raises must NOT propagate — trace capture never
        breaks the LLM call path."""
        monkeypatch.setattr(settings, "trace_capture_enabled", True, raising=False)

        class _BrokenDB:
            async def execute(self, *a, **kw):
                raise RuntimeError("relation \"llm_traces\" does not exist")

            async def commit(self):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        set_current_request("generate", prompt="p")
        resp = SimpleNamespace(
            provider="ollama", model="m", text="t",
            tokens_prompt=1, tokens_completion=1, total_duration_ms=1,
            success=True, error=None, tool_calls=[],
        )
        with patch("app.database.async_session", lambda: _BrokenDB()):
            await record_trace(resp)  # must not raise


# ---------------------------------------------------------------------------
# model_router wiring
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestModelRouterWiring:
    def test_begin_trace_stashes_snapshot(self, monkeypatch):
        """model_router._begin_trace feeds the same ContextVar record_trace
        reads — the round-trip the _record_call hook relies on."""
        monkeypatch.setattr(settings, "trace_capture_enabled", True, raising=False)
        from app import model_router

        ctx = copy_context()

        def _drive():
            model_router._begin_trace(
                "generate", prompt="hello", system="sys",
                temperature=0.9, max_tokens=128,
            )
            return current_request.get()

        snap = ctx.run(_drive)
        assert snap["kind"] == "generate"
        assert snap["prompt"] == "hello"
        assert snap["max_tokens"] == 128

    async def test_record_call_invokes_record_trace(self, monkeypatch):
        """_record_call is the single post-call chokepoint; it must fan out to
        record_trace so every dispatch path is captured."""
        monkeypatch.setattr(settings, "trace_capture_enabled", True, raising=False)
        from app import model_router

        called = {}

        async def _fake_record_trace(resp):
            called["resp"] = resp

        # Neutralize the cost-path so this test isolates the trace hook.
        async def _noop(*a, **kw):
            return None

        monkeypatch.setattr(
            "app.utils.trace_capture.record_trace", _fake_record_trace,
        )
        monkeypatch.setattr(
            "app.utils.cost_tracking.record_llm_call", _noop, raising=False,
        )

        resp = SimpleNamespace(
            provider="ollama", model="m", text="t", success=True,
        )
        out = await model_router._record_call(resp)
        assert out is resp
        assert called["resp"] is resp
