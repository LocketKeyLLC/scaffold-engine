"""Sprint W.6 — tests for model_router.tool_call().

Covers the four dispatch paths:
  - role= + native-tools provider → provider.tool_call()
  - role= + non-tools provider    → coaxing fallback (chat + JSON parse)
  - model= + native-tools provider → provider.tool_call() via legacy path
  - model= + non-tools provider    → coaxing fallback via legacy path
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app import model_router
from app.providers.base import ModelResponse, Tool, ToolCall


def _ok(tool_calls: list[ToolCall] | None = None, text: str = ""):
    """Shape a successful provider response."""
    return ModelResponse(
        text=text, model="fake", success=True,
        tool_calls=tool_calls or [],
    )


def _fail(error: str = "boom"):
    return ModelResponse(
        text="", model="fake", success=False, error=error,
    )


SAMPLE_TOOL = Tool(
    name="record_verification",
    description="Record a pass/fail verification verdict.",
    input_schema={
        "type": "object",
        "properties": {
            "pass": {"type": "boolean"},
            "reason": {"type": "string"},
            "confidence": {"type": "number"},
        },
        "required": ["pass", "reason", "confidence"],
    },
)


@pytest.mark.smoke
class TestToolCallRoleNativePath:
    """role= + provider.supports_native_tools=True → delegate to tool_call."""

    async def test_role_native_provider_returns_tool_calls(self):
        provider = MagicMock()
        provider.supports_native_tools = True
        provider.tool_call = AsyncMock(return_value=_ok(
            tool_calls=[ToolCall(id="t0", name="record_verification",
                                  arguments={"pass": True, "reason": "ok",
                                             "confidence": 0.9})],
        ))

        with patch.object(
            model_router, "_resolve_role",
            return_value=("qwen2.5:7b", provider),
        ):
            resp = await model_router.tool_call(
                messages=[{"role": "user", "content": "task X"}],
                tools=[SAMPLE_TOOL],
                role="model_verifier",
            )

        assert resp.success
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].arguments == {
            "pass": True, "reason": "ok", "confidence": 0.9,
        }
        provider.tool_call.assert_awaited_once()

    async def test_role_native_provider_failure_enriched(self):
        """Failure on the role path gets formatted with role+provider context."""
        provider = MagicMock()
        provider.supports_native_tools = True
        provider.name = "openai"
        provider.tool_call = AsyncMock(return_value=_fail("401 Unauthorized"))

        with patch.object(
            model_router, "_resolve_role",
            return_value=("gpt-4o", provider),
        ):
            resp = await model_router.tool_call(
                messages=[{"role": "user", "content": "x"}],
                tools=[SAMPLE_TOOL],
                role="model_verifier",
            )

        assert not resp.success
        # _format_provider_error decorates with [role=... provider=...]
        assert "role=model_verifier" in (resp.error or "")
        assert "401" in (resp.error or "")


@pytest.mark.smoke
class TestToolCallRoleCoaxingFallback:
    """role= + supports_native_tools=False → coaxing fallback."""

    async def test_coaxes_via_chat_when_provider_lacks_tools(self):
        provider = MagicMock()
        provider.supports_native_tools = False
        chat_payload = {"pass": False, "reason": "missing X", "confidence": 0.8}
        provider.chat_completion = AsyncMock(return_value=_ok(
            text=json.dumps(chat_payload),
        ))

        with patch.object(
            model_router, "_resolve_role",
            return_value=("model-x", provider),
        ):
            resp = await model_router.tool_call(
                messages=[{"role": "user", "content": "verify"}],
                tools=[SAMPLE_TOOL],
                role="model_verifier",
            )

        assert resp.success
        # Synthesized ToolCall from JSON parse.
        assert len(resp.tool_calls) == 1
        assert resp.tool_calls[0].id == "coaxed_0"
        assert resp.tool_calls[0].name == "record_verification"
        assert resp.tool_calls[0].arguments == chat_payload
        # The coaxing system message was prepended.
        called_messages = provider.chat_completion.await_args.args[1]
        assert called_messages[0]["role"] == "system"
        assert "record_verification" in called_messages[0]["content"]
        assert "input schema" in called_messages[0]["content"].lower()

    async def test_coaxing_unparseable_response_keeps_empty_tool_calls(self):
        """If the chat response isn't valid JSON, tool_calls stays empty.
        Caller sees empty list and treats it as 'no tool selected'."""
        provider = MagicMock()
        provider.supports_native_tools = False
        provider.chat_completion = AsyncMock(return_value=_ok(text="not JSON {{{"))

        with patch.object(
            model_router, "_resolve_role",
            return_value=("model-x", provider),
        ):
            resp = await model_router.tool_call(
                messages=[{"role": "user", "content": "x"}],
                tools=[SAMPLE_TOOL],
                role="model_verifier",
            )

        assert resp.success
        assert resp.tool_calls == []

    async def test_coaxing_chat_failure_propagates_with_role_context(self):
        provider = MagicMock()
        provider.supports_native_tools = False
        provider.chat_completion = AsyncMock(return_value=_fail("429 rate-limited"))

        with patch.object(
            model_router, "_resolve_role",
            return_value=("model-x", provider),
        ):
            resp = await model_router.tool_call(
                messages=[{"role": "user", "content": "x"}],
                tools=[SAMPLE_TOOL],
                role="model_verifier",
            )

        assert not resp.success
        assert "role=model_verifier" in (resp.error or "")


@pytest.mark.smoke
class TestToolCallLegacyModelPath:
    """model= bypasses role lookup, goes through ollama provider."""

    async def test_model_path_uses_ollama_provider_native(self):
        provider = MagicMock()
        provider.supports_native_tools = True
        provider.tool_call = AsyncMock(return_value=_ok(
            tool_calls=[ToolCall(id="t0", name="record_verification",
                                  arguments={"pass": True, "reason": "ok",
                                             "confidence": 0.95})],
        ))
        with patch("app.providers.get_provider", return_value=provider):
            resp = await model_router.tool_call(
                messages=[{"role": "user", "content": "x"}],
                tools=[SAMPLE_TOOL],
                model="qwen2.5:7b",
            )
        assert resp.success
        assert resp.tool_calls[0].arguments["pass"] is True

    async def test_role_and_model_collision_rejected(self):
        with pytest.raises(ValueError, match="either role= .* or model="):
            await model_router.tool_call(
                messages=[{"role": "user", "content": "x"}],
                tools=[SAMPLE_TOOL],
                role="model_verifier",
                model="qwen2.5:7b",
            )


@pytest.mark.smoke
class TestToolCallEmptyTools:
    """Empty tools list short-circuits to a plain chat call (no schema injection)."""

    async def test_empty_tools_falls_through_to_chat(self):
        provider = MagicMock()
        provider.supports_native_tools = False  # so we exercise coaxing path
        provider.chat_completion = AsyncMock(return_value=_ok(text="hi"))

        with patch.object(
            model_router, "_resolve_role",
            return_value=("model-x", provider),
        ):
            resp = await model_router.tool_call(
                messages=[{"role": "user", "content": "x"}],
                tools=[],  # empty
                role="model_verifier",
            )
        # No coaxing system was prepended (just the original message).
        called_messages = provider.chat_completion.await_args.args[1]
        assert len(called_messages) == 1
        assert called_messages[0]["content"] == "x"
        assert resp.tool_calls == []
