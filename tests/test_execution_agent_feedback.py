"""Sprint W.1 — verifier-feedback loop on retry.

Covers `_format_reviewer_feedback`, the `_build_prompt` retry-injection
path, and `_set_node_status` writing the reason to the new column.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules.execution_agent import (
    _build_prompt,
    _format_reviewer_feedback,
    _set_node_status,
)


# ---------------------------------------------------------------------------
# _format_reviewer_feedback
# ---------------------------------------------------------------------------


def test_feedback_empty_on_first_attempt():
    """retry_count == 0 must never render a feedback block, even if a stale
    reason is on the row (e.g., from a manual cleanup or migration)."""
    assert _format_reviewer_feedback({
        "retry_count": 0,
        "last_verification_reason": "stale leftover reason",
    }) == ""


def test_feedback_empty_when_no_reason():
    """retry_count > 0 alone is not enough — the reason must exist."""
    assert _format_reviewer_feedback({
        "retry_count": 2,
        "last_verification_reason": None,
    }) == ""


def test_feedback_empty_when_reason_whitespace():
    assert _format_reviewer_feedback({
        "retry_count": 1,
        "last_verification_reason": "   \n\t  ",
    }) == ""


def test_feedback_renders_with_attempt_number_and_reason():
    out = _format_reviewer_feedback({
        "retry_count": 1,
        "last_verification_reason": "Only one algorithm mentioned, task requires three",
    })
    assert "Reviewer feedback (attempt 2)" in out
    assert "Only one algorithm mentioned" in out
    assert out.endswith("---\n\n")  # separator before the real prompt


def test_feedback_attempt_counter_increments_for_third_try():
    out = _format_reviewer_feedback({
        "retry_count": 2,
        "last_verification_reason": "still incomplete",
    })
    assert "attempt 3" in out


# ---------------------------------------------------------------------------
# _build_prompt — retry-injection integration
# ---------------------------------------------------------------------------


def test_build_prompt_first_attempt_no_block():
    node = {
        "title": "Plan refactor",
        "prompt_template": "Plan the refactor in 3 steps.",
        "retry_count": 0,
        "last_verification_reason": None,
    }
    out = _build_prompt(node, {"description": "Improve the markdown linter"})
    assert "Reviewer feedback" not in out
    assert "Plan the refactor in 3 steps." in out


def test_build_prompt_retry_prepends_block():
    node = {
        "title": "Plan refactor",
        "prompt_template": "Plan the refactor in 3 steps.",
        "retry_count": 1,
        "last_verification_reason": "Output gave 1 step, task asked for 3",
    }
    out = _build_prompt(node, {"description": "Improve the markdown linter"})
    feedback_idx = out.find("Reviewer feedback")
    template_idx = out.find("Plan the refactor in 3 steps.")
    assert feedback_idx >= 0
    assert template_idx >= 0
    # Feedback must come BEFORE the template body so the model sees it as
    # a top-level instruction.
    assert feedback_idx < template_idx
    assert "Output gave 1 step" in out


def test_build_prompt_retry_with_no_template_still_works():
    """The fallback (no prompt_template) path also picks up the feedback."""
    node = {
        "title": "List 3 algorithms",
        "prompt_template": None,
        "retry_count": 1,
        "last_verification_reason": "Only one algorithm given",
    }
    out = _build_prompt(node, {"description": "Sort study"})
    assert "Reviewer feedback" in out
    assert "Execute this task: List 3 algorithms" in out


def test_build_prompt_retry_count_zero_with_reason_still_no_block():
    """Defensive — a non-empty reason with retry_count=0 must NOT inject.
    The retry path is the only legitimate way the column should be read."""
    node = {
        "title": "T1",
        "prompt_template": "do the thing",
        "retry_count": 0,
        "last_verification_reason": "something",
    }
    out = _build_prompt(node, {})
    assert "Reviewer feedback" not in out


# ---------------------------------------------------------------------------
# _set_node_status — verification_reason persistence
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_node_status_writes_verification_reason():
    db = AsyncMock()
    db.commit = AsyncMock()

    await _set_node_status(
        db, "node-uuid", "failed",
        output="some output",
        optimized_prompt="some prompt",
        verification_reason="verifier said: too vague",
    )

    db.execute.assert_called_once()
    args, _ = db.execute.call_args
    sql_obj, params = args  # text(...) clause + params dict
    assert "last_verification_reason" in str(sql_obj)
    assert params["verification_reason"] == "verifier said: too vague"
    assert params["status"] == "failed"


@pytest.mark.asyncio
async def test_set_node_status_passes_none_when_caller_omits_reason():
    """Backwards compat: non-W.1 callers (e.g., 'done' transitions) leave
    the reason untouched via COALESCE(NULL, prior_value).
    """
    db = AsyncMock()
    db.commit = AsyncMock()

    await _set_node_status(db, "node-uuid", "done", output="great output")

    args, _ = db.execute.call_args
    _, params = args
    assert params["verification_reason"] is None
