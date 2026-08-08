"""§17.621 (audit #20) — handoff_policy auto-handoff trigger in POST /assist/{sid}/submit.

The auto values (auto_on_skip / auto_all_remaining) were stored + echoed but never
consumed. On a SKIP with a non-manual policy the router now delegates to the
autonomous executor via a background handoff instead of leaving the step skipped.
These tests exercise the router-level trigger (the executor itself is covered by
the handoff_step tests + a live smoke).
"""
from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from app.routers import assist as assist_router
from app.routers.assist import AssistSubmitInput


def _quiet_submit_side_effects():
    """§17.689/§17.709/§17.710 — silence the submit-path side effects these
    handoff/learning tests don't exercise (decision deliberation, facts
    distillation, unified-memory capture + grounding). They were added to the
    submit handler after these tests were written and otherwise run real DB/LLM
    work against the bare AsyncMock db. Router-level gate settings, so flipping
    them off skips the calls entirely — and makes the tests hermetic against the
    container's live valve env."""
    return (
        patch.object(assist_router.settings, "assist_decision_deliberation_enabled", False),
        patch.object(assist_router.settings, "assist_capture_facts_enabled", False),
        patch.object(assist_router.settings, "assist_unified_memory_enabled", False),
        # §17.731 — the container now runs with the block valves ON (compose);
        # these legacy tests assert the commit-despite-verdict behavior, so pin
        # the valves OFF to stay hermetic against the live env (same §17.710d
        # lesson: pytest runs inside the valve-on container).
        patch.object(assist_router.settings, "assist_block_on_failed_verify", False),
        patch.object(assist_router.settings, "assist_block_on_incomplete_verify", False),
    )


def _skip_body(node_key="T1"):
    return AssistSubmitInput(node_key=node_key, action="skip")


def _patch(policy):
    """Patch get_session (active + given policy), spawn, and submit_step."""
    return (
        patch.object(assist_router.assist_agent, "get_session",
                     new=AsyncMock(return_value={"status": "active", "handoff_policy": policy})),
        patch.object(assist_router.assist_agent, "spawn_handoff_background"),
        patch.object(assist_router.assist_agent, "submit_step",
                     new=AsyncMock(return_value={"status": "skipped", "committed": True})),
    )


@pytest.mark.asyncio
async def test_skip_auto_on_skip_spawns_single_handoff():
    gs, sp, ss = _patch("auto_on_skip")
    with gs, sp as spawn, ss as submit:
        result = await assist_router.assist_submit("sid-1", _skip_body(), AsyncMock())
    assert result["status"] == "auto_handoff"
    assert result["handoff_mode"] == "single"
    spawn.assert_called_once()
    assert spawn.call_args.kwargs["mode"] == "single"
    assert spawn.call_args.kwargs["node_key"] == "T1"
    submit.assert_not_called()  # the step is delegated, NOT skipped


@pytest.mark.asyncio
async def test_skip_auto_all_remaining_spawns_all_remaining():
    gs, sp, ss = _patch("auto_all_remaining")
    with gs, sp as spawn, ss as submit:
        result = await assist_router.assist_submit("sid-1", _skip_body(), AsyncMock())
    assert result["handoff_mode"] == "all_remaining"
    assert spawn.call_args.kwargs["mode"] == "all_remaining"
    submit.assert_not_called()


@pytest.mark.asyncio
async def test_skip_manual_policy_does_not_handoff():
    gs, sp, ss = _patch("manual")
    with gs, sp as spawn, ss as submit:
        result = await assist_router.assist_submit("sid-1", _skip_body(), AsyncMock())
    spawn.assert_not_called()
    submit.assert_called_once()  # normal skip path
    assert result["status"] == "skipped"


@pytest.mark.asyncio
async def test_submit_action_never_auto_handoffs():
    """A normal submit (not skip) never triggers auto-handoff, even under an auto policy."""
    with contextlib.ExitStack() as es:
        for p in _quiet_submit_side_effects():
            es.enter_context(p)
        es.enter_context(patch.object(assist_router.assist_agent, "get_session",
                         new=AsyncMock(return_value={"status": "active", "handoff_policy": "auto_on_skip"})))
        spawn = es.enter_context(patch.object(assist_router.assist_agent, "spawn_handoff_background"))
        es.enter_context(patch.object(assist_router.settings, "assist_verify_on_submit", False))
        es.enter_context(patch.object(assist_router.assist_agent, "learn_from_submit",
                         new=AsyncMock(return_value=None)))
        submit = es.enter_context(patch.object(assist_router.assist_agent, "submit_step",
                         new=AsyncMock(return_value={"status": "committed"})))
        result = await assist_router.assist_submit(
            "sid-1", AssistSubmitInput(node_key="T1", action="submit", output="did it"), AsyncMock(),
        )
    spawn.assert_not_called()
    submit.assert_called_once()
    assert result["status"] == "committed"


# ── §17.644 — don't learn substitutions from failed/unrelated evidence ──────


def _submit_body(node_key="T1"):
    return AssistSubmitInput(node_key=node_key, action="submit",
                             output="the 4TB drive is partitioned")


def _patch_submit(outcome):
    """Active session, a verdict with the given outcome, a clean commit, and a
    learn stub — so a test can assert whether learn_from_submit ran."""
    return (
        patch.object(assist_router.assist_agent, "get_session",
                     new=AsyncMock(return_value={"status": "active", "handoff_policy": "manual"})),
        patch.object(assist_router.assist_agent, "verify_submit_outcome",
                     new=AsyncMock(return_value={"outcome": outcome, "reason": "r"})),
        patch.object(assist_router.assist_agent, "submit_step",
                     new=AsyncMock(return_value={"status": "committed"})),
        patch.object(assist_router.assist_agent, "learn_from_submit",
                     new=AsyncMock(return_value={"STORAGE": "4TB"})),
    )


@pytest.mark.asyncio
async def test_failed_verdict_suppresses_substitution_learning():
    """A `failed` verdict means the evidence is unrelated to this step; learning
    from it produces garbage subs (STORAGE=4TB from '4TB drive'). Skip it — the
    step still commits (block valve off by default), just without learning."""
    with contextlib.ExitStack() as es:
        gs, ver, ss, learn = _patch_submit("failed")
        for p in (gs, ver, ss, *_quiet_submit_side_effects()):
            es.enter_context(p)
        learn_mock = es.enter_context(learn)
        result = await assist_router.assist_submit("sid-1", _submit_body(), AsyncMock())
    assert result["status"] == "committed"
    learn_mock.assert_not_called()
    assert "learned_substitutions" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["succeeded", "unclear"])
async def test_nonfailed_verdict_still_learns(outcome):
    """`succeeded`/`unclear` (and None) still learn — only a definite `failed`
    verdict suppresses it, so the §17.644 guard doesn't over-block."""
    with contextlib.ExitStack() as es:
        gs, ver, ss, learn = _patch_submit(outcome)
        for p in (gs, ver, ss, *_quiet_submit_side_effects()):
            es.enter_context(p)
        learn_mock = es.enter_context(learn)
        result = await assist_router.assist_submit("sid-1", _submit_body(), AsyncMock())
    learn_mock.assert_called_once()
    assert result["learned_substitutions"] == {"STORAGE": "4TB"}
