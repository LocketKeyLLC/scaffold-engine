"""Tests for app/modules/prompt_optimizer.py (#9.22)."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import prompt_optimizer as po


# ---------------------------------------------------------------------------
# _deterministic_strip — filler/whitespace removal
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_deterministic_strip_collapses_multispace():
    assert po._deterministic_strip("hello    there") == "hello there"


@pytest.mark.smoke
def test_deterministic_strip_collapses_triple_newlines():
    result = po._deterministic_strip("line1\n\n\n\nline2")
    assert "\n\n\n" not in result


@pytest.mark.smoke
def test_deterministic_strip_is_idempotent():
    text = "Write a clear, concise answer."
    once = po._deterministic_strip(text)
    twice = po._deterministic_strip(once)
    assert once == twice


# ---------------------------------------------------------------------------
# _analyze — issues surface
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_analyze_flags_non_imperative_opening():
    result = po._analyze("the user wants something")
    assert any("imperative" in i.lower() for i in result.issues)


@pytest.mark.smoke
def test_analyze_flags_hedging():
    result = po._analyze("Maybe try to write something, kind of like this")
    assert any("hedging" in i.lower() for i in result.issues)


@pytest.mark.smoke
def test_analyze_flags_oversized_prompts():
    long = "x" * 2500
    result = po._analyze(long)
    assert any("2000 chars" in i for i in result.issues)


@pytest.mark.smoke
def test_analyze_clean_imperative_has_minimal_issues():
    """A well-structured imperative prompt should have at most size-based issues."""
    result = po._analyze("Summarize the input document in under 100 words.")
    assert result.has_imperative_structure is True
    # No hedging, no filler — only possibly imperative issue (shouldn't flag)
    assert result.hedge_count == 0


# ---------------------------------------------------------------------------
# _clarity_score — math
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_clarity_score_zero_when_original_empty():
    assert po._clarity_score(0, 0, 0, 0, True) == 0.0


@pytest.mark.smoke
def test_clarity_score_peaks_when_fully_optimized():
    """100% reduction + all issues resolved + intent preserved == 1.0."""
    score = po._clarity_score(100, 0, 10, 0, True)
    assert score == 1.0


@pytest.mark.smoke
def test_clarity_score_intent_failure_drops_score():
    """Loss of intent caps the clarity component."""
    with_intent = po._clarity_score(100, 50, 5, 1, True)
    without_intent = po._clarity_score(100, 50, 5, 1, False)
    assert with_intent > without_intent


@pytest.mark.smoke
def test_clarity_score_is_clamped_to_one():
    """Score never exceeds 1.0 even if all inputs peak."""
    assert po._clarity_score(1000, 0, 100, 0, True) <= 1.0


# ---------------------------------------------------------------------------
# _llm_verify — parse-verify chain
# ---------------------------------------------------------------------------
@pytest.mark.smoke
async def test_llm_verify_accepts_structured_true():
    fake_resp = SimpleNamespace(text='{"preserved": true, "reason": "all intent kept"}')
    with patch.object(po.model_router, "chat", AsyncMock(return_value=fake_resp)):
        preserved, reason = await po._llm_verify("orig", "opt", "m")
    assert preserved is True
    assert "intent" in reason


@pytest.mark.smoke
async def test_llm_verify_accepts_structured_false():
    fake_resp = SimpleNamespace(text='{"preserved": false, "reason": "scope changed"}')
    with patch.object(po.model_router, "chat", AsyncMock(return_value=fake_resp)):
        preserved, reason = await po._llm_verify("orig", "opt", "m")
    assert preserved is False


@pytest.mark.smoke
async def test_llm_verify_handles_markdown_fenced_json():
    fake_resp = SimpleNamespace(text='```json\n{"preserved": true, "reason": "ok"}\n```')
    with patch.object(po.model_router, "chat", AsyncMock(return_value=fake_resp)):
        preserved, _ = await po._llm_verify("orig", "opt", "m")
    assert preserved is True


# ---------------------------------------------------------------------------
# optimize_prompt — top-level flow
# ---------------------------------------------------------------------------
@pytest.mark.smoke
async def test_optimize_prompt_rolls_back_on_verify_failure():
    """#6.12 — when verify fails, fall back to pre_cleaned AND surface intent_preserved=False."""
    with patch.object(
        po, "_llm_optimize",
        AsyncMock(return_value="CHANGED output that may lose intent"),
    ), patch.object(
        po, "_llm_verify",
        AsyncMock(return_value=(False, "scope mismatch")),
    ):
        result = await po.optimize_prompt("Write something clear please.")
    assert result.intent_preserved is False
    # optimized must have rolled back to pre_cleaned (not the LLM output)
    assert result.optimized_prompt != "CHANGED output that may lose intent"


@pytest.mark.smoke
async def test_optimize_prompt_skip_verify_sets_verifier_used_to_skipped():
    with patch.object(po, "_llm_optimize", AsyncMock(return_value="cleaned output")):
        result = await po.optimize_prompt("Please write a thing.", skip_verify=True)
    assert result.verifier_used == "skipped"
    assert result.intent_preserved is True


@pytest.mark.smoke
async def test_optimize_prompt_populates_all_result_fields():
    with patch.object(po, "_llm_optimize", AsyncMock(return_value="tight output")), \
         patch.object(po, "_llm_verify", AsyncMock(return_value=(True, "ok"))):
        result = await po.optimize_prompt("Maybe try to write something long.")
    assert result.original_prompt.startswith("Maybe")
    assert result.optimized_prompt == "tight output"
    assert result.token_count_before > 0
    assert result.token_count_after > 0
    assert isinstance(result.issues_found, list)
