"""Tests for app.modules.prompt_optimizer._llm_verify.

Sprint X.10 — rewritten for the model_router.tool_call migration.
Pre-X.10 tests exercised parse_json_object + regex fallback chains
that no longer exist in the verifier (wrapper handles structured-output
parsing internally). The fail-closed contract is unchanged: any failure
returns (False, "") so a corrupted optimization is never silently
accepted.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _tool_call_resp(*, success: bool = True, args: dict | None = None,
                    no_calls: bool = False):
    """Build a ModelResponse-shaped MagicMock for tool_call returns.

    - success=False                 → wrapper-level failure (dispatch error)
    - no_calls=True                 → response succeeded but no tool was called
    - args=dict                     → exactly one ToolCall with these arguments
    - args=None and no_calls=False  → success but tool_calls is empty list
    """
    r = MagicMock()
    r.success = success
    if no_calls or args is None:
        r.tool_calls = []
    else:
        call = MagicMock()
        call.arguments = args
        r.tool_calls = [call]
    return r


@pytest.mark.smoke
class TestLLMVerifyHappyPath:
    """tool_call returns valid structured args → verdict surfaces verbatim."""

    def test_preserved_true_with_reason(self):
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.tool_call = AsyncMock(return_value=_tool_call_resp(
                args={"preserved": True, "reason": "all intent intact"},
            ))
            preserved, reason = _run(_llm_verify("orig", "opt"))
        assert preserved is True
        assert "intact" in reason

    def test_preserved_false_with_reason(self):
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.tool_call = AsyncMock(return_value=_tool_call_resp(
                args={"preserved": False, "reason": "scope narrowed"},
            ))
            preserved, reason = _run(_llm_verify("orig", "opt"))
        assert preserved is False
        assert "narrowed" in reason

    def test_preserved_true_no_reason_field(self):
        """The schema marks `reason` optional; missing it is fine."""
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.tool_call = AsyncMock(return_value=_tool_call_resp(
                args={"preserved": True},
            ))
            preserved, reason = _run(_llm_verify("orig", "opt"))
        assert preserved is True
        assert reason == ""

    def test_reason_truncated_at_200_chars(self):
        """Long reasons are clamped server-side to 200 chars (matches the
        pre-X.10 behavior — operators never want a 5000-char `reason`)."""
        from app.modules.prompt_optimizer import _llm_verify
        long_reason = "x" * 500
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.tool_call = AsyncMock(return_value=_tool_call_resp(
                args={"preserved": True, "reason": long_reason},
            ))
            _, reason = _run(_llm_verify("orig", "opt"))
        assert len(reason) == 200


@pytest.mark.smoke
class TestLLMVerifyFailClosed:
    """Any failure path must return (False, "") so a corrupt
    optimization is never silently accepted. The X.10 migration
    preserves this contract; these tests are the regression guard."""

    def test_no_tool_calls_returns_false(self):
        """Wrapper succeeded but model didn't emit a tool call —
        unparseable signal, fail closed."""
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.tool_call = AsyncMock(return_value=_tool_call_resp(no_calls=True))
            preserved, reason = _run(_llm_verify("orig", "opt"))
        assert preserved is False
        assert reason == ""

    def test_missing_preserved_key_returns_false(self):
        """Tool was called but args lack the required `preserved` key —
        schema violation, fail closed rather than guessing."""
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.tool_call = AsyncMock(return_value=_tool_call_resp(
                args={"reason": "I forgot the verdict"},
            ))
            preserved, reason = _run(_llm_verify("orig", "opt"))
        assert preserved is False
        assert reason == ""

    def test_dispatch_failure_returns_false(self):
        """ModelResponse.success=False (dispatch error, retry exhausted) —
        no verdict at all, fail closed."""
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.tool_call = AsyncMock(return_value=_tool_call_resp(
                success=False, args={"preserved": True},  # success=False ignores args
            ))
            preserved, reason = _run(_llm_verify("orig", "opt"))
        assert preserved is False, (
            "success=False must fail closed even if args claim preserved=true — "
            "we cannot trust args from a non-successful response."
        )

    def test_args_not_dict_returns_false(self):
        """Pathological provider return shape (args was a list/string) —
        read_tool_args defends; verifier fails closed."""
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            r = MagicMock()
            r.success = True
            call = MagicMock()
            call.arguments = ["not", "a", "dict"]
            r.tool_calls = [call]
            mr.tool_call = AsyncMock(return_value=r)
            preserved, _ = _run(_llm_verify("orig", "opt"))
        assert preserved is False


@pytest.mark.smoke
class TestLLMVerifyContract:
    """Smoke: the call site invokes tool_call, not chat — the X.10
    migration. If a future refactor regresses this, the test catches it."""

    def test_uses_tool_call_not_chat(self):
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.tool_call = AsyncMock(return_value=_tool_call_resp(
                args={"preserved": True},
            ))
            mr.chat = AsyncMock(side_effect=AssertionError(
                "_llm_verify must NOT use model_router.chat after X.10 — "
                "use tool_call so the wrapper handles structured-output parsing"
            ))
            _run(_llm_verify("orig", "opt"))
        # tool_call was invoked exactly once.
        assert mr.tool_call.await_count == 1

    def test_passes_record_verification_tool(self):
        """The tool schema must be the canonical RECORD_VERIFICATION_TOOL —
        if a future change forks the schema, this test surfaces it."""
        from app.modules.prompt_optimizer import _llm_verify, RECORD_VERIFICATION_TOOL
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.tool_call = AsyncMock(return_value=_tool_call_resp(
                args={"preserved": True},
            ))
            _run(_llm_verify("orig", "opt"))
        # _llm_verify passes tools= as a keyword arg.
        kwargs = mr.tool_call.await_args.kwargs
        assert "tools" in kwargs
        assert RECORD_VERIFICATION_TOOL in kwargs["tools"]

    def test_dispatches_via_role_not_model(self):
        """§17.89 Pattern 3 — _llm_verify must route through role= so the
        configured MODEL_VERIFIER_PROVIDER is honored. Pre-§17.89 the helper
        passed model= directly which always went through the Ollama provider."""
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.tool_call = AsyncMock(return_value=_tool_call_resp(
                args={"preserved": True},
            ))
            _run(_llm_verify("orig", "opt"))
        kwargs = mr.tool_call.await_args.kwargs
        assert kwargs.get("role") == "model_verifier"
        assert "model" not in kwargs
