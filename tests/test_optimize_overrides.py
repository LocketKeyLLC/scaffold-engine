"""Regression tests for:

#6.11  optimize_prompt must thread model_overrides through to role resolution
       so per-request model selection is respected.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run(coro):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _mock_resp(text_content: str = "optimized output"):
    r = MagicMock()
    r.text = text_content
    return r


@pytest.mark.smoke
class TestOptimizeModelOverrides:
    """#6.11: optimize_prompt wires model_overrides into get_model lookups."""

    def test_model_overrides_consulted_for_default(self):
        """When model_optimizer/verifier unset, get_model must receive model_overrides."""
        from app.modules.prompt_optimizer import optimize_prompt
        overrides = {"model_verifier": "fake:1b"}

        with patch("app.modules.prompt_optimizer.model_router") as mr, \
             patch("app.modules.prompt_optimizer.get_model",
                   return_value="fake:1b") as mock_gm:
            mr.chat = AsyncMock(return_value=_mock_resp("clean prompt"))

            _run(optimize_prompt(
                prompt="please kindly build a tool",
                skip_verify=True,
                model_overrides=overrides,
            ))

        # get_model must have been asked with the overrides dict
        call_kwargs_set = any(
            overrides in c.args for c in mock_gm.call_args_list
        )
        assert call_kwargs_set, (
            f"get_model never received the overrides dict. "
            f"Calls: {mock_gm.call_args_list}"
        )

    def test_explicit_model_optimizer_wins_over_overrides(self):
        """Explicit model_optimizer param must take precedence over get_model lookup.

        §17.89 contract update: post-Pattern-3-migration, optimize_prompt no
        longer passes `model=` to model_router. Instead it folds the explicit
        ``model_optimizer`` into the per-call overrides dict under the
        ``model_general`` key and dispatches via ``role="model_general"``,
        letting provider_for_role's override precedence pick up the explicit
        tag. The test asserts the new path: overrides kwarg carries the tag,
        and `model_router.chat` receives `role`+`overrides` (never `model`).
        """
        from app.modules.prompt_optimizer import optimize_prompt

        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.chat = AsyncMock(return_value=_mock_resp("clean prompt"))

            _run(optimize_prompt(
                prompt="please build",
                model_optimizer="explicit-tag:7b",
                skip_verify=True,
                model_overrides={"model_verifier": "from-overrides:1b"},
            ))

        # Exactly one chat call (skip_verify=True skips _llm_verify).
        assert mr.chat.await_count == 1
        kwargs = mr.chat.await_args.kwargs
        assert kwargs.get("role") == "model_general"
        assert "model" not in kwargs
        # The explicit model_optimizer was folded into the overrides dict
        # under the role's settings field name.
        assert kwargs.get("overrides", {}).get("model_general") == "explicit-tag:7b"

    def test_no_overrides_still_works(self):
        """Omitting model_overrides must not raise (backward compat)."""
        from app.modules.prompt_optimizer import optimize_prompt

        with patch("app.modules.prompt_optimizer.model_router") as mr, \
             patch("app.modules.prompt_optimizer.get_model",
                   return_value="default:7b"):
            mr.chat = AsyncMock(return_value=_mock_resp("clean"))

            # No model_overrides arg — should still work
            result = _run(optimize_prompt(
                prompt="build something",
                skip_verify=True,
            ))

        assert result.optimized_prompt == "clean"
