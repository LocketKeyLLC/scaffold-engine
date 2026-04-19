"""Tests for app.modules.prompt_optimizer._llm_verify hardening.

Verifies the fail-closed parsing chain:
  1. Primary JSON parse succeeds → use parsed verdict
  2. JSON fails, regex fallback finds preserved:true|false → use that
  3. Both fail → return False (fail closed)
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


def _mock_resp(text_content: str):
    r = MagicMock()
    r.text = text_content
    return r


@pytest.mark.smoke
class TestLLMVerifyJSONPath:
    """Primary: clean JSON response."""

    def test_preserved_true_from_json(self):
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.chat = AsyncMock(return_value=_mock_resp(
                '{"preserved": true, "reason": "all intent intact"}'
            ))
            preserved, reason = _run(_llm_verify("orig", "opt", "verifier"))
        assert preserved is True
        assert "intact" in reason

    def test_preserved_false_from_json(self):
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.chat = AsyncMock(return_value=_mock_resp(
                '{"preserved": false, "reason": "scope narrowed"}'
            ))
            preserved, reason = _run(_llm_verify("orig", "opt", "verifier"))
        assert preserved is False
        assert "narrowed" in reason

    def test_json_with_markdown_fences(self):
        """parse_json_object should strip ```json fences."""
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.chat = AsyncMock(return_value=_mock_resp(
                '```json\n{"preserved": true, "reason": "ok"}\n```'
            ))
            preserved, _ = _run(_llm_verify("orig", "opt", "verifier"))
        assert preserved is True


@pytest.mark.smoke
class TestLLMVerifyRegexFallback:
    """Fallback: malformed JSON but regex-extractable verdict."""

    def test_regex_catches_preserved_true(self):
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            # Verdict present but inside prose — JSON parse will fail
            mr.chat = AsyncMock(return_value=_mock_resp(
                "Here is my analysis: preserved: true because the scope is intact."
            ))
            preserved, _ = _run(_llm_verify("orig", "opt", "verifier"))
        assert preserved is True

    def test_regex_catches_preserved_false(self):
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.chat = AsyncMock(return_value=_mock_resp(
                "Analysis: preserved=false, the constraint was dropped."
            ))
            preserved, _ = _run(_llm_verify("orig", "opt", "verifier"))
        assert preserved is False

    def test_regex_case_insensitive(self):
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.chat = AsyncMock(return_value=_mock_resp(
                "PRESERVED: TRUE — all good."
            ))
            preserved, _ = _run(_llm_verify("orig", "opt", "verifier"))
        assert preserved is True


@pytest.mark.smoke
class TestLLMVerifyFailClosed:
    """Fail closed: unparseable output must return False, not True."""

    def test_garbage_returns_false(self):
        """Old bug: substring 'true' anywhere returned True. New: must fail closed."""
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.chat = AsyncMock(return_value=_mock_resp(
                "the sky is blue and grass is green"  # no preserved: anything
            ))
            preserved, _ = _run(_llm_verify("orig", "opt", "verifier"))
        assert preserved is False, (
            "Unparseable verifier output must default to False (fail closed), "
            "not True (which would accept corrupted optimizations silently)."
        )

    def test_trap_substring_true_returns_false(self):
        """Old heuristic: 'true' substring → True. New: no 'preserved' keyword → False."""
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.chat = AsyncMock(return_value=_mock_resp(
                "It is true that I cannot assess this prompt."
            ))
            preserved, _ = _run(_llm_verify("orig", "opt", "verifier"))
        assert preserved is False, (
            "Naked 'true' in prose must not be treated as a positive verdict."
        )

    def test_empty_response_returns_false(self):
        from app.modules.prompt_optimizer import _llm_verify
        with patch("app.modules.prompt_optimizer.model_router") as mr:
            mr.chat = AsyncMock(return_value=_mock_resp(""))
            preserved, _ = _run(_llm_verify("orig", "opt", "verifier"))
        assert preserved is False
