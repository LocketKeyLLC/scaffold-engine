"""
Black-box tests for execution_agent._verify_output.

Sprint W.6 contract (post-tool-call migration):
  - Returns tuple[Literal["pass","fail"], reason: str, confidence: float]
  - "pass"   : verifier returned tool_calls[0].arguments["pass"] == True
  - "fail"   : verifier said pass=False OR ANY error path (no tool call,
               args not a dict, missing 'pass' key, tool_call exception,
               unsuccessful response, timeout, unexpected error)
  - Never returns "skipped" — skip decision is made by the CALLER, not here.
  - Single-call structured output via app.model_router.tool_call.

Pre-W.6 these tests targeted parse_json_object behavior (markdown-fenced,
think-tagged, preamble-prefixed text). Those cases now live in
tests/test_model_router_tool_call.py — the verifier itself just reads
the structured args.
"""
import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import execution_agent
from app.providers.base import ModelResponse, ToolCall


def _ok_with_args(args: dict | None):
    """ModelResponse with tool_calls populated from args (or empty)."""
    tool_calls = []
    if args is not None:
        tool_calls = [ToolCall(
            id="t0", name="record_verification", arguments=args,
        )]
    return ModelResponse(
        text="", model="fake", success=True, tool_calls=tool_calls,
    )


def _fail_response(error: str):
    return ModelResponse(text="", model="fake", success=False, error=error)


@pytest.fixture
def patch_tool_call():
    """Yield a mock model_router.tool_call() with configurable behavior."""
    with patch.object(
        execution_agent.model_router, "tool_call", new=AsyncMock(),
    ) as m:
        yield m


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------
class TestPassPath:
    @pytest.mark.asyncio
    async def test_pass_true(self, patch_tool_call):
        patch_tool_call.return_value = _ok_with_args({
            "pass": True, "reason": "ok", "confidence": 0.9,
        })
        status, reason, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "pass"
        assert reason == "ok"
        assert conf == 0.9

    @pytest.mark.asyncio
    async def test_pass_with_extra_fields_tolerated(self, patch_tool_call):
        """Extra fields in args are ignored (forward-compat)."""
        patch_tool_call.return_value = _ok_with_args({
            "pass": True, "reason": "good", "confidence": 1.0,
            "future_field": "ignored",
        })
        status, _, _ = await execution_agent._verify_output("t", "o", "m")
        assert status == "pass"


# ---------------------------------------------------------------------------
# Verifier said fail — pass-through
# ---------------------------------------------------------------------------
class TestFailPath:
    @pytest.mark.asyncio
    async def test_pass_false_returns_fail(self, patch_tool_call):
        patch_tool_call.return_value = _ok_with_args({
            "pass": False, "reason": "bad output", "confidence": 0.2,
        })
        status, reason, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "fail"
        assert reason == "bad output"
        assert conf == 0.2


# ---------------------------------------------------------------------------
# All error paths — MUST return "fail", never "skipped"
# ---------------------------------------------------------------------------
class TestFailClosedOnErrors:
    @pytest.mark.asyncio
    async def test_no_tool_call_emitted(self, patch_tool_call):
        """Model declined to call the tool; coaxing parse may have failed."""
        patch_tool_call.return_value = _ok_with_args(None)  # tool_calls=[]
        status, reason, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "fail"
        assert "no tool call" in reason.lower()
        assert conf == 0.0

    @pytest.mark.asyncio
    async def test_missing_pass_key(self, patch_tool_call):
        patch_tool_call.return_value = _ok_with_args({
            "reason": "forgot pass key", "confidence": 0.5,
        })
        status, reason, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "fail"
        assert "pass" in reason.lower()
        assert conf == 0.0

    @pytest.mark.asyncio
    async def test_unsuccessful_response_returns_fail(self, patch_tool_call):
        patch_tool_call.return_value = _fail_response("429 rate-limited")
        status, reason, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "fail"
        assert "rate-limited" in reason.lower() or "response error" in reason.lower()
        assert conf == 0.0

    @pytest.mark.asyncio
    async def test_tool_call_raises_returns_fail(self, patch_tool_call):
        patch_tool_call.side_effect = RuntimeError("connection refused")
        status, reason, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "fail"
        assert "connection" in reason.lower() or "call failed" in reason.lower()
        assert conf == 0.0

    @pytest.mark.asyncio
    async def test_confidence_coerced_when_not_numeric(self, patch_tool_call):
        """Non-numeric confidence (rare LLM glitch on coaxing) → 0.0, no crash."""
        patch_tool_call.return_value = _ok_with_args({
            "pass": True, "reason": "ok", "confidence": "high",
        })
        status, _, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "pass"
        assert conf == 0.0


# ---------------------------------------------------------------------------
# Timeout path
# ---------------------------------------------------------------------------
class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_fail(self, patch_tool_call, monkeypatch):
        async def _hang(*_a, **_kw):
            await asyncio.sleep(5)
            return _ok_with_args({"pass": True, "reason": "", "confidence": 1.0})

        patch_tool_call.side_effect = _hang
        monkeypatch.setattr(execution_agent.settings, "verify_timeout_seconds", 0.1)
        status, reason, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "fail"
        assert "timeout" in reason.lower()
        assert conf == 0.0
