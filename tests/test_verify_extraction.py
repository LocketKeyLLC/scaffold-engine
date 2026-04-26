"""
Black-box tests for execution_agent._verify_output.

Contract (post-fail-closed refactor):
  - Returns tuple[Literal["pass","fail"], reason: str, confidence: float]
  - "pass"   : verifier returned {"pass": true, ...}
  - "fail"   : verifier said fail OR ANY error path (empty, parse failure,
               missing schema, chat exception, timeout, unexpected error)
  - Never returns "skipped" — skip decision is made by the CALLER, not here.
  - Single-call parsing via app.utils.llm_parsing.parse_json_object.
"""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import execution_agent


def _resp(text: str):
    return SimpleNamespace(text=text)


@pytest.fixture
def patch_chat():
    """Yield a mock chat() with configurable return/side_effect."""
    with patch.object(execution_agent.model_router, "chat", new=AsyncMock()) as m:
        yield m


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------
class TestPassPath:
    @pytest.mark.asyncio
    async def test_clean_pass_true(self, patch_chat):
        patch_chat.return_value = _resp('{"pass": true, "reason": "ok", "confidence": 0.9}')
        status, reason, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "pass"
        assert reason == "ok"
        assert conf == 0.9

    @pytest.mark.asyncio
    async def test_markdown_fenced_pass(self, patch_chat):
        patch_chat.return_value = _resp('```json\n{"pass": true, "reason": "", "confidence": 1.0}\n```')
        status, _, _ = await execution_agent._verify_output("t", "o", "m")
        assert status == "pass"

    @pytest.mark.asyncio
    async def test_think_tags_stripped_then_pass(self, patch_chat):
        patch_chat.return_value = _resp('<think>deliberating</think>\n{"pass": true, "reason": "good", "confidence": 0.8}')
        status, reason, _ = await execution_agent._verify_output("t", "o", "m")
        assert status == "pass"
        assert reason == "good"

    @pytest.mark.asyncio
    async def test_preamble_then_json(self, patch_chat):
        patch_chat.return_value = _resp('Here is my verdict: {"pass": true, "reason": "fine", "confidence": 0.7}')
        status, _, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "pass"
        assert conf == 0.7


# ---------------------------------------------------------------------------
# Verifier said fail — pass-through
# ---------------------------------------------------------------------------
class TestFailPath:
    @pytest.mark.asyncio
    async def test_pass_false_returns_fail(self, patch_chat):
        patch_chat.return_value = _resp('{"pass": false, "reason": "bad output", "confidence": 0.2}')
        status, reason, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "fail"
        assert reason == "bad output"
        assert conf == 0.2


# ---------------------------------------------------------------------------
# All error paths — MUST return "fail", never "skipped"
# ---------------------------------------------------------------------------
class TestFailClosedOnErrors:
    @pytest.mark.asyncio
    async def test_empty_response(self, patch_chat):
        patch_chat.return_value = _resp("")
        status, reason, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "fail"
        assert "empty" in reason.lower()
        assert conf == 0.0

    @pytest.mark.asyncio
    async def test_whitespace_only(self, patch_chat):
        patch_chat.return_value = _resp("   \n\t  ")
        status, _, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "fail"
        assert conf == 0.0

    @pytest.mark.asyncio
    async def test_only_think_no_json(self, patch_chat):
        patch_chat.return_value = _resp("<think>nothing outside</think>")
        status, _, _ = await execution_agent._verify_output("t", "o", "m")
        assert status == "fail"

    @pytest.mark.asyncio
    async def test_total_garbage(self, patch_chat):
        patch_chat.return_value = _resp("this is not JSON at all lol")
        status, _, _ = await execution_agent._verify_output("t", "o", "m")
        assert status == "fail"

    @pytest.mark.asyncio
    async def test_missing_pass_key(self, patch_chat):
        patch_chat.return_value = _resp('{"reason": "forgot pass key", "confidence": 0.5}')
        status, reason, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "fail"
        assert "pass" in reason.lower()
        assert conf == 0.0

    @pytest.mark.asyncio
    async def test_chat_raises_returns_fail(self, patch_chat):
        patch_chat.side_effect = RuntimeError("connection refused")
        status, reason, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "fail"
        assert "chat failed" in reason.lower() or "connection" in reason.lower()
        assert conf == 0.0


# ---------------------------------------------------------------------------
# Timeout path
# ---------------------------------------------------------------------------
class TestTimeout:
    @pytest.mark.asyncio
    async def test_timeout_returns_fail(self, patch_chat, monkeypatch):
        async def _hang(*_a, **_kw):
            await asyncio.sleep(5)
            return _resp('{"pass": true}')

        patch_chat.side_effect = _hang
        # Force a tiny timeout
        monkeypatch.setattr(execution_agent.settings, "verify_timeout_seconds", 0.1)
        status, reason, conf = await execution_agent._verify_output("t", "o", "m")
        assert status == "fail"
        assert "timeout" in reason.lower()
        assert conf == 0.0
