"""
test_verify_extraction.py — Unit tests for the 5-layer JSON extraction pipeline
in execution_agent._verify_output() and _extract_verify_result().

Covers:
  Layer 1: <think>/<thinking> tag stripping (closed, unclosed, nested)
  Layer 2: Markdown code fence extraction
  Layer 3: Direct JSON parse (fast path)
  Layer 4: json_repair fallback for malformed JSON
  Layer 5: Brace-find + repair for preamble text
  Schema:  _extract_verify_result — missing keys, valid, extras
  Edge:    Empty response, all-think, unparseable, pass=false
"""
import pytest
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_chat_response(raw_text: str):
    """Return an AsyncMock that simulates model_router.chat() output."""
    mock = AsyncMock(return_value=SimpleNamespace(text=raw_text))
    return mock


def _run(coro):
    """Run async coroutine synchronously."""
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# Layer 1: Think-tag stripping
# ---------------------------------------------------------------------------

import pytest

@pytest.mark.smoke
class TestLayer1ThinkStrip:
    """<think> and <thinking> blocks must be removed before JSON parsing."""

    def test_closed_think_tags(self):
        raw = '<think>I need to evaluate this carefully.</think>{"pass": true, "reason": "correct", "confidence": 0.95}'
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is True
        assert reason == "correct"
        assert conf == 0.95

    def test_closed_thinking_tags(self):
        raw = '<thinking>Let me reason step by step.</thinking>{"pass": false, "reason": "missing algorithms", "confidence": 0.88}'
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is False
        assert "missing" in reason.lower()

    def test_unclosed_think_truncated(self):
        raw = '<think>This reasoning was truncated by token limit and never clo'
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        # Should skip gracefully (no content after stripping)
        assert passed is True
        assert "skipped" in reason.lower()
        assert conf == 0.0

    def test_multiline_think_with_json_after(self):
        raw = (
            "<think>\nStep 1: Check if task is met.\n"
            "Step 2: The output contains the required info.\n"
            "Verdict: pass.\n</think>\n"
            '{"pass": true, "reason": "all requirements present", "confidence": 0.92}'
        )
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is True
        assert conf == 0.92

    def test_only_think_no_answer(self):
        raw = "<think>The task asks for sorting algorithms but the output only discusses bubble sort.</think>"
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is True
        assert "skipped" in reason.lower()
        assert conf == 0.0


# ---------------------------------------------------------------------------
# Layer 2: Markdown fence extraction
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestLayer2MarkdownFence:
    """JSON inside ```json ... ``` fences must be extracted."""

    def test_json_fence(self):
        raw = '```json\n{"pass": true, "reason": "looks good", "confidence": 0.91}\n```'
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is True
        assert reason == "looks good"

    def test_bare_fence(self):
        raw = '```\n{"pass": false, "reason": "incomplete", "confidence": 0.70}\n```'
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is False
        assert conf == 0.70

    def test_think_then_fence(self):
        raw = (
            "<think>Let me check the requirements.</think>\n"
            '```json\n{"pass": true, "reason": "requirements met", "confidence": 0.88}\n```'
        )
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is True
        assert conf == 0.88


# ---------------------------------------------------------------------------
# Layer 3: Direct JSON parse (fast path)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestLayer3DirectParse:
    """Clean JSON should parse on first try with no extraction needed."""

    def test_clean_json(self):
        raw = '{"pass": true, "reason": "three algorithms listed", "confidence": 0.95}'
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is True
        assert conf == 0.95

    def test_pass_false(self):
        raw = '{"pass": false, "reason": "only one algorithm mentioned", "confidence": 0.90}'
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is False
        assert "one algorithm" in reason


# ---------------------------------------------------------------------------
# Layer 4: json_repair fallback
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestLayer4JsonRepair:
    """Malformed JSON should be repaired by json_repair library."""

    def test_trailing_comma(self):
        raw = '{"pass": true, "reason": "good output", "confidence": 0.85,}'
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is True
        assert conf == 0.85

    def test_single_quotes(self):
        raw = "{'pass': true, 'reason': 'acceptable', 'confidence': 0.80}"
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is True


# ---------------------------------------------------------------------------
# Layer 5: Brace-find + repair
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestLayer5BraceFind:
    """Preamble text before JSON should be skipped via brace-find."""

    def test_preamble_text(self):
        raw = 'Here is my analysis of the output:\n{"pass": true, "reason": "meets requirements", "confidence": 0.93}'
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is True
        assert conf == 0.93

    def test_preamble_with_malformed_json(self):
        raw = 'Based on my evaluation:\n{"pass": true, "reason": "correct implementation", "confidence": 0.87,}'
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is True
        assert conf == 0.87

    def test_think_then_preamble_then_json(self):
        raw = (
            "<think>Checking requirements carefully.</think>\n"
            "After analysis:\n"
            '{"pass": false, "reason": "function signature missing", "confidence": 0.91}'
        )
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is False
        assert "missing" in reason.lower()


# ---------------------------------------------------------------------------
# Schema validation: _extract_verify_result
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestExtractVerifyResult:
    """_extract_verify_result must handle missing keys and type coercion."""

    def test_valid_schema(self):
        from app.modules.execution_agent import _extract_verify_result
        passed, reason, conf = _extract_verify_result(
            {"pass": True, "reason": "all good", "confidence": 0.95}
        )
        assert passed is True
        assert reason == "all good"
        assert conf == 0.95

    def test_missing_pass_key(self):
        from app.modules.execution_agent import _extract_verify_result
        passed, reason, conf = _extract_verify_result(
            {"result": True, "reason": "good"}  # wrong key name
        )
        # Should treat as skip
        assert passed is True
        assert "skipped" in reason.lower()
        assert conf == 0.0

    def test_extra_keys_ignored(self):
        from app.modules.execution_agent import _extract_verify_result
        passed, reason, conf = _extract_verify_result(
            {"pass": False, "reason": "bad", "confidence": 0.8, "notes": "extra field"}
        )
        assert passed is False
        assert conf == 0.8

    def test_missing_optional_fields(self):
        from app.modules.execution_agent import _extract_verify_result
        passed, reason, conf = _extract_verify_result({"pass": True})
        assert passed is True
        assert reason == ""
        assert conf == 0.0

    def test_string_confidence_coerced(self):
        from app.modules.execution_agent import _extract_verify_result
        passed, reason, conf = _extract_verify_result(
            {"pass": True, "reason": "ok", "confidence": "0.75"}
        )
        assert conf == 0.75


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestEdgeCases:
    """Empty responses, total garbage, whitespace-only."""

    def test_empty_response(self):
        with patch("app.model_router.chat", _mock_chat_response("")):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is True
        assert "skipped" in reason.lower()

    def test_whitespace_only(self):
        with patch("app.model_router.chat", _mock_chat_response("   \n\t  ")):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is True
        assert "skipped" in reason.lower()

    def test_total_garbage(self):
        with patch("app.model_router.chat", _mock_chat_response("lorem ipsum dolor sit amet")):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        assert passed is True
        assert "skipped" in reason.lower()
        assert conf == 0.0

    def test_nested_think_with_json_inside(self):
        """JSON inside think tags should NOT be extracted — only post-think JSON counts."""
        raw = '<think>{"pass": false, "reason": "bad"}</think>{"pass": true, "reason": "good", "confidence": 0.9}'
        with patch("app.model_router.chat", _mock_chat_response(raw)):
            from app.modules.execution_agent import _verify_output
            passed, reason, conf = _run(_verify_output("test task", "test output", "qwen2.5:7b"))
        # The post-think JSON should win, not the one inside think tags
        assert passed is True
        assert reason == "good"
