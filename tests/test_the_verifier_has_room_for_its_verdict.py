"""§17.1751 — the verifier is given room to reach its verdict.

Live (job 2772acae T3, 2026-10-22): the CodeGen verifier (glm-5.3-flash:cloud) spent the router's default 4096 tokens
deliberating in all six draws -- `done_reason='length'`, no tool call -- so the step failed "verifier produced no tool
call" three retries running without its output ever being judged. Replayed with 12k it answered in 4030 and 5831.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.config import settings
from app.modules import execution_verify as ev


def _verdict():
    return SimpleNamespace(success=True, error=None, text="",
                           tool_calls=[SimpleNamespace(name="record_verification",
                                                       arguments={"pass": False, "reason": "wrong file", "confidence": 0.8})])


class _NoCache:
    async def get(self, *a):
        return None

    async def put(self, *a):
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("call", [
    lambda: ev._verify_codegen_output("Implement SEC EDGAR client", "```python\nx = 1\n```", brief_goal="lab"),
    lambda: ev._verify_output("Write the report", "the report"),
])
async def test_both_verifiers_ask_with_the_verifier_budget(call):
    tc = AsyncMock(return_value=_verdict())
    with patch.object(ev.model_router, "tool_call", tc), patch.object(ev, "get_verifier_cache", return_value=_NoCache()):
        status, reason, _ = await call()
    assert (status, reason) == ("fail", "wrong file")
    assert tc.await_args.kwargs["max_tokens"] == settings.verifier_max_tokens


def test_the_budget_is_above_what_the_live_verdicts_needed():
    """4096 starved every draw; the replayed verdicts needed 4030 and 5831 completion tokens."""
    assert settings.verifier_max_tokens >= 8192
    assert type(settings).model_fields["verifier_max_tokens"].default == 12288
