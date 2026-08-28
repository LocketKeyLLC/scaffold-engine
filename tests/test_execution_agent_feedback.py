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
    execute_next_node,
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


# ---------------------------------------------------------------------------
# §17.854 (audit A1) — expected_status guard on _set_node_status stops an
# orphaned executor from overwriting a cleanup-marked node.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_set_node_status_default_has_no_status_predicate():
    """Without expected_status the write is byte-compatible: no status guard,
    no bind param — every existing caller is unchanged."""
    from unittest.mock import MagicMock
    db = AsyncMock()
    db.commit = AsyncMock()
    res = MagicMock()
    res.fetchone.return_value = ("node-uuid",)
    db.execute.return_value = res

    updated = await _set_node_status(db, "node-uuid", "done", output="x")

    args, _ = db.execute.call_args
    sql_obj, params = args
    assert "AND status = :expected" not in str(sql_obj)
    assert "expected" not in params
    assert updated is True


@pytest.mark.asyncio
async def test_set_node_status_expected_status_blocks_stale_write():
    """expected_status='running' + a node no longer running → 0 rows updated,
    returns False, so the caller discards the stale result instead of flipping
    a cleanup-marked 'failed' node back to 'done'."""
    from unittest.mock import MagicMock
    db = AsyncMock()
    db.commit = AsyncMock()
    res = MagicMock()
    res.fetchone.return_value = None  # WHERE ... AND status='running' matched nothing
    db.execute.return_value = res

    updated = await _set_node_status(
        db, "node-uuid", "done", output="stale", expected_status="running",
    )

    args, _ = db.execute.call_args
    sql_obj, params = args
    assert "AND status = :expected" in str(sql_obj)
    assert params["expected"] == "running"
    assert updated is False


# ---------------------------------------------------------------------------
# W.1 integration — `execute_next_node` wires retry state from the DB row
# through `_build_prompt` and into the LLM call.
# ---------------------------------------------------------------------------
#
# The unit tests above prove `_format_reviewer_feedback` and `_build_prompt`
# work in isolation, and that `_set_node_status` persists the reason. The
# missing piece was: does `execute_next_node` actually carry retry_count +
# last_verification_reason from the SQL row through to the prompt the model
# receives? Without that wiring, the persisted reason is dead text.
#
# This test patches the row-claim seam (`_get_next_node`), captures what
# the model is asked, and asserts the feedback block landed in the user
# message. It deliberately short-circuits before verifier/persistence so
# the test surface is the wiring contract, not the full execute lifecycle.


from contextlib import asynccontextmanager
from unittest.mock import patch
from app.modules import execution_agent


@asynccontextmanager
async def _fake_session(db):
    """Build a context-manager-shaped fake for `async with async_session()`."""
    yield db


def _fake_session_factory(db):
    """Module-level patcher: replace `async_session` with a callable that
    returns the context manager. Mirrors the pattern in test_execution_agent_*."""
    return lambda: _fake_session(db)


@pytest.mark.asyncio
async def test_execute_next_node_wires_retry_feedback_through_to_llm_prompt():
    """Retry-state row → `_build_prompt` → LLM input must contain the W.1
    block. Patches the LLM call to capture the prompt and short-circuit."""
    job_id = "11111111-2222-3333-4444-555555555555"

    captured_messages = {}

    class _ShortCircuit(Exception):
        """Raised after capture to bail early with a known signal."""

    async def _capture_and_bail(messages, model=None, **kw):
        # `messages = [{"role": "system", ...}, {"role": "user", ...}]`
        captured_messages["all"] = messages
        raise _ShortCircuit()

    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    job_row = {
        "id": job_id, "status": "running",
        "refined_brief": {"description": "Build a bash linter"},
    }
    # Row state: this node failed once, verifier said the output was wrong.
    # On retry, the prompt MUST carry that rejection reason forward.
    node_row = {
        "id": "node-uuid",
        "node_key": "T1",
        "title": "List 3 algorithms",
        "tool": "LLM",
        "prompt_template": "Name three sorting algorithms.",
        "domain": None,
        "depends_on": [],
        "assigned_model": None,
        "retry_count": 1,
        "last_verification_reason": "Only one algorithm given; task asked for three",
    }

    with patch.object(
        execution_agent, "async_session", _fake_session_factory(db),
    ), patch.object(
        execution_agent, "_get_job", AsyncMock(return_value=job_row),
    ), patch.object(
        execution_agent, "_get_next_node", AsyncMock(return_value=node_row),
    ), patch.object(
        execution_agent, "_fetch_upstream_outputs", AsyncMock(return_value={}),
    ), patch.object(
        execution_agent, "_fetch_rag_context", AsyncMock(return_value=None),
    ), patch.object(
        execution_agent, "_log_execution", AsyncMock(),
    ), patch.object(
        execution_agent, "_set_node_status", AsyncMock(),
    ), patch.object(
        execution_agent.model_router, "chat", _capture_and_bail,
    ):
        # skip_optimize keeps optimize_prompt out of the picture.
        # The captured _ShortCircuit exception bubbles up through the
        # execute path's exception handler, which writes 'failed' and
        # returns a failed-shape dict — we don't assert on that.
        result = await execute_next_node(job_id, skip_optimize=True)

    # Result is a failed-shape because we raised _ShortCircuit; we don't
    # care about that. The contract is what the model SAW.
    assert "all" in captured_messages, "model_router.chat was never invoked"
    user_msg = next(
        m["content"] for m in captured_messages["all"] if m["role"] == "user"
    )
    assert "Reviewer feedback (attempt 2)" in user_msg, (
        "W.1 feedback block missing — execute_next_node didn't wire "
        "retry_count + last_verification_reason from the row into the "
        f"user prompt. Got prompt:\n{user_msg[:500]}"
    )
    assert "Only one algorithm given" in user_msg, (
        "rejection reason from last_verification_reason must appear "
        "verbatim in the prompt — that's the entire point of W.1."
    )
    # And a sanity check: the original task prompt is still there too.
    assert "Name three sorting algorithms." in user_msg


@pytest.mark.asyncio
async def test_execute_next_node_first_attempt_has_no_feedback_block():
    """Counterpart: a first-attempt row (retry_count=0) must NOT inject the
    block, even if last_verification_reason has stale content from a prior
    cleanup or hand-edited row."""
    job_id = "22222222-2222-3333-4444-555555555555"
    captured_messages = {}

    class _ShortCircuit(Exception):
        pass

    async def _capture_and_bail(messages, model=None, **kw):
        captured_messages["all"] = messages
        raise _ShortCircuit()

    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    job_row = {
        "id": job_id, "status": "running",
        "refined_brief": {"description": "x"},
    }
    node_row = {
        "id": "node-uuid",
        "node_key": "T1",
        "title": "Plan task",
        "tool": "LLM",
        "prompt_template": "Plan it.",
        "domain": None,
        "depends_on": [],
        "assigned_model": None,
        "retry_count": 0,  # ← first attempt
        "last_verification_reason": "stale leftover from migration",
    }

    with patch.object(
        execution_agent, "async_session", _fake_session_factory(db),
    ), patch.object(
        execution_agent, "_get_job", AsyncMock(return_value=job_row),
    ), patch.object(
        execution_agent, "_get_next_node", AsyncMock(return_value=node_row),
    ), patch.object(
        execution_agent, "_fetch_upstream_outputs", AsyncMock(return_value={}),
    ), patch.object(
        execution_agent, "_fetch_rag_context", AsyncMock(return_value=None),
    ), patch.object(
        execution_agent, "_log_execution", AsyncMock(),
    ), patch.object(
        execution_agent, "_set_node_status", AsyncMock(),
    ), patch.object(
        execution_agent.model_router, "chat", _capture_and_bail,
    ):
        await execute_next_node(job_id, skip_optimize=True)

    user_msg = next(
        m["content"] for m in captured_messages["all"] if m["role"] == "user"
    )
    assert "Reviewer feedback" not in user_msg
    assert "stale leftover" not in user_msg


# ---------------------------------------------------------------------------
# §17.854 (audit A3) — _build_prompt delegates to prompt_assembly so autonomous
# nodes see the §17.850 brief essentials (constraints/inputs/answers), and the
# two prompt paths can't drift.
# ---------------------------------------------------------------------------

def test_build_prompt_includes_brief_constraints():
    """The autonomous path must now carry brief constraints — the §17.844/845
    blindness the shared module was built to prevent."""
    node = {"title": "Set up the server", "prompt_template": "Do the setup.",
            "retry_count": 0, "last_verification_reason": None}
    brief = {
        "description": "Stand up a web app",
        "constraints": ["PRESERVE the existing install", "do not reformat /dev/sda"],
        "inputs_available": ["Proxmox host at 10.0.0.5"],
        "user_feedback": "use the existing postgres, don't install a new one",
    }
    out = _build_prompt(node, brief)
    assert "PRESERVE the existing install" in out
    assert "Proxmox host at 10.0.0.5" in out
    assert "use the existing postgres" in out


def test_build_prompt_matches_prompt_assembly_plus_feedback():
    """Parity guard: _build_prompt == build_base_prompt (+ optional reviewer
    feedback prepend). If someone re-forks a local copy, this fails."""
    from app.modules.prompt_assembly import build_base_prompt
    from app.modules.execution_agent import _build_prompt as bp
    node = {"title": "T", "prompt_template": "tpl", "retry_count": 0,
            "last_verification_reason": None}
    brief = {"description": "d", "constraints": ["c1"]}
    # no reviewer feedback (retry_count 0) → identical to build_base_prompt
    assert bp(node, brief) == build_base_prompt(node, brief)
