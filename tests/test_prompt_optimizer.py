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
# _llm_verify coverage moved to tests/test_prompt_optimizer_verify.py
# (Sprint X.10 — verifier migrated to model_router.tool_call). Three
# JSON-parse-chain tests removed here; their tool_call equivalents live
# in the dedicated file.
# ---------------------------------------------------------------------------


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
async def test_optimize_prompt_falls_back_when_llm_returns_empty():
    """§17.462 — a thinking model (model_general post-§17.440) can return
    success + empty content. optimize_prompt must NEVER hand back a blank
    optimized prompt (that blanked node-execution prompts → blocked jobs).
    It falls back to the deterministically-stripped, non-empty text."""
    for empty in ("", "   ", "\n\t "):
        with patch.object(po, "_llm_optimize", AsyncMock(return_value=empty)):
            result = await po.optimize_prompt(
                "Build a CLI to-do manager with JSON persistence.",
                skip_verify=True,
            )
        assert result.optimized_prompt.strip(), f"blank optimized for input {empty!r}"
        # Falls back to a stripped form of the original intent, not garbage.
        assert "to-do" in result.optimized_prompt.lower()


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


# ---------------------------------------------------------------------------
# Regressions: think-tag leak + semantic-shift filler removal (TASK 7-fix)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_llm_optimize_strips_think_tags():
    """_llm_optimize must drop <think>...</think> blocks from model output."""
    fake_resp = SimpleNamespace(text="<think>internal reasoning</think>Write a summary.")
    with patch.object(po.model_router, "chat", new=AsyncMock(return_value=fake_resp)) as m:
        out = await po._llm_optimize("raw prompt")
    assert "<think>" not in out
    assert "internal reasoning" not in out
    assert out == "Write a summary."
    # §17.89 — verify the helper now dispatches via role= rather than model=.
    _, kwargs = m.call_args
    assert kwargs.get("role") == "model_general"
    assert "model" not in kwargs


@pytest.mark.smoke
def test_deterministic_strip_preserves_very():
    """'very' is semantically load-bearing — must survive deterministic strip."""
    text = "Write a very specific answer about Rust ownership."
    out = po._deterministic_strip(text)
    assert "very" in out.lower()


@pytest.mark.smoke
def test_deterministic_strip_preserves_quite_rather_somewhat():
    """Other semantic-shift hedges preserved too."""
    text = "The result is quite rather somewhat complete."
    out = po._deterministic_strip(text)
    for word in ("quite", "rather", "somewhat"):
        assert word in out.lower(), f"{word} was stripped"
