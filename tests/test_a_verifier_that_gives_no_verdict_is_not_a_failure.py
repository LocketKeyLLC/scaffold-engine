"""§17.1844 — a verifier that returns no verdict has not judged the output; another model is asked once.

Live (follow-up 5b4880c0 T6U8, 2026-10-27): the attempt passed every engine check and the sandbox; glm-5.3-flash spent
its 12,288-token budget deliberating in six draws, returned no tool call, and the step -- on its last attempt -- failed
on "verifier produced no tool call" (7 of 526 failures in 7 days).
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.modules import execution_verify as ev

MSGS = [{"role": "user", "content": "TASK: fix\n\nOUTPUT:\nx = 1"}]
NO_VERDICT = SimpleNamespace(success=True, tool_calls=[], text="Let me analyze this task carefully...", error=None)
PASS = SimpleNamespace(success=True, text="", error=None,
                       tool_calls=[SimpleNamespace(arguments={"pass": True, "reason": "complete", "confidence": 0.8})])


class _NoCache:
    async def get(self, *a):
        return None

    async def put(self, *a):
        return None


async def _run(responses, **kw):
    call = AsyncMock(side_effect=responses)
    with patch.object(ev.model_router, "tool_call", new=call), \
         patch.object(ev, "get_verifier_cache", return_value=_NoCache()):
        out = await ev._run_verification(MSGS, **kw)
    return out, [c.kwargs["role"] for c in call.call_args_list]


async def test_no_verdict_asks_the_fallback_verifier_once():
    out, roles = await _run([NO_VERDICT, PASS])
    assert out == ("pass", "complete", 0.8) and roles == ["model_verifier", "model_coder"]


async def test_no_verdict_from_both_fails_and_says_so():
    (status, reason, _), roles = await _run([NO_VERDICT, NO_VERDICT])
    assert status == "fail" and roles == ["model_verifier", "model_coder"]
    assert reason.startswith("verifier produced no tool call (and from the fallback verifier, ")


async def test_a_real_verdict_is_never_second_guessed():
    fail = SimpleNamespace(success=True, text="", error=None,
                           tool_calls=[SimpleNamespace(arguments={"pass": False, "reason": "wrong", "confidence": 0.9})])
    out, roles = await _run([fail])
    assert out == ("fail", "wrong", 0.9) and roles == ["model_verifier"]


async def test_no_fallback_when_none_is_set_or_it_is_the_verifier_itself():
    with patch.object(ev.settings, "verifier_fallback_role", ""):
        (status, reason, _), roles = await _run([NO_VERDICT])
    assert (status, reason, roles) == ("fail", "verifier produced no tool call", ["model_verifier"])
    (_s, _r, _c), roles = await _run([NO_VERDICT], role="model_coder")
    assert roles == ["model_coder"]
