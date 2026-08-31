"""§17.890 — the operator's explicit word outranks the verifier.

Live failure: the operator told the engine — repeatedly — that they had
completed the step; decide routed to submit, the §17.731 success verifier
judged the bare claim as if it were pasted evidence ('incomplete': a claim
shows no deliverable), the blocking valve refused the commit, and the §17.884
continuation walked them into a fix flow for finished work. Three layers fix
it, tested here:

1. `assist_policy.looks_like_completion_claim` — the deterministic,
   precision-first claim detector.
2. `assist_policy._override` — a claim routed to question/ask/note/status/
   advance is overridden to submit (so it records AND advances).
3. `assist_submit` — a verify-blocked (incomplete/failed) submit whose output
   is a bare claim commits anyway, verdict tagged `operator_affirmed`.
"""
from __future__ import annotations

import contextlib
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import assist_policy as P
from app.routers import assist as assist_router
from app.routers.assist import AssistSubmitInput


# ── 1. the claim detector ────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    "done",
    "Done.",
    "finished",
    "I did that already",
    "I've completed what you asked",
    "yes I did that",
    "it's installed",
    "its working now",
    "that's done",
    "task is complete",
    "everything is set up",
    "I already installed docker",
    "it worked",
    "all set",
    "we're good",
    "I just ran it and it's done",
])
def test_completion_claim_positive(msg):
    assert P.looks_like_completion_claim(msg) is True


@pytest.mark.parametrize("msg", [
    "",  # empty
    "done?",  # a question is never a claim
    "is it done",  # how-to/should-style question shape
    "how do I know it's done",  # help-seeking (no question mark needed)
    "I did that but it failed",  # failure wording
    "it's not done yet",  # negation
    "I haven't finished the install",  # negation
    "when it's done I'll tell you",  # a plan, not a claim
    "once that's finished we can move on",  # a plan, not a claim
    "root@pve:~# apt install docker\ndone",  # paste-shaped → evidence path
    "error: it's done downloading but the service is broken",  # failure wording
    "x" * 300,  # long messages are reports, not claims
    "the 4TB drive is partitioned",  # a fact, not a completion claim
    "run apt update first",  # instruction
    "help me finish this",  # help request
])
def test_completion_claim_negative(msg):
    assert P.looks_like_completion_claim(msg) is False


def test_completion_claim_smart_punct_normalized():
    # §17.692 — a curly apostrophe from a phone must still match
    assert P.looks_like_completion_claim("it’s done") is True


# ── 2. the decide post-filter override ───────────────────────────────────────

def _decision(action, signals=None):
    return {"action": action, "evidence": "", "error_text": "", "query": "",
            "note_text": "", "note_kind": "note", "plan_impact": "none",
            "suggestion": None, "confidence": "high", "rationale": "r",
            "signals": signals or {}}


@pytest.mark.parametrize("action", ["question", "ask", "note", "status", "advance"])
def test_override_claim_routes_to_submit(action):
    out = P.apply_deterministic_overrides(_decision(action), "I did that already")
    assert out["action"] == "submit"
    assert out["override"] == "completion_claim"
    assert out["evidence"] == "I did that already"
    assert out["confidence"] == "high"


def test_override_claim_leaves_submit_alone():
    out = P.apply_deterministic_overrides(_decision("submit"), "it's done")
    assert "override" not in out  # unchanged decision object


def test_override_claim_leaves_skip_and_fix_alone():
    for action in ("skip", "fix", "pause", "finalize"):
        out = P.apply_deterministic_overrides(_decision(action), "it's done")
        assert out["action"] == action


def test_override_shell_paste_still_wins_over_claim():
    # A clean shell paste that also says "done" is EVIDENCE — the §17.855
    # shell-result gate owns it (submit with the paste as evidence).
    sig = {"shell_paste": True, "shell_error": False, "last_assistant_was_fix": False}
    out = P.apply_deterministic_overrides(_decision("question", sig),
                                          "root@pve:~# systemctl enable x\ndone")
    assert out["action"] == "submit"
    assert out["override"] == "shell_result"


# ── 3. the router unblock ────────────────────────────────────────────────────

def _es_patches(es, outcome):
    for p in (
        patch.object(assist_router.settings, "assist_decision_deliberation_enabled", False),
        patch.object(assist_router.settings, "assist_capture_facts_enabled", False),
        patch.object(assist_router.settings, "assist_unified_memory_enabled", False),
        patch.object(assist_router.settings, "assist_verify_on_submit", True),
        patch.object(assist_router.settings, "assist_block_on_failed_verify", True),
        patch.object(assist_router.settings, "assist_block_on_incomplete_verify", True),
        patch.object(assist_router.assist_agent, "get_session",
                     new=AsyncMock(return_value={"status": "active",
                                                 "handoff_policy": "manual"})),
        patch.object(assist_router.assist_agent, "ingest_turn", new=AsyncMock()),
        patch.object(assist_router.assist_agent, "capture_execution_context",
                     new=AsyncMock(return_value=None)),
        patch.object(assist_router.assist_agent, "verify_submit_outcome",
                     new=AsyncMock(return_value={"outcome": outcome,
                                                 "reason": "no deliverable shown"})),
        patch.object(assist_router.assist_agent, "learn_from_submit",
                     new=AsyncMock(return_value=None)),
    ):
        es.enter_context(p)
    friction = es.enter_context(patch.object(
        assist_router.assist_agent, "record_friction", new=AsyncMock()))
    submit = es.enter_context(patch.object(
        assist_router.assist_agent, "submit_step",
        new=AsyncMock(return_value={"status": "committed"})))
    return friction, submit


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["incomplete", "failed"])
async def test_blocked_verdict_with_claim_commits_operator_affirmed(outcome):
    with contextlib.ExitStack() as es:
        friction, submit = _es_patches(es, outcome)
        result = await assist_router.assist_submit(
            "sid-1",
            AssistSubmitInput(node_key="T1", action="submit",
                              output="I did that already"),
            AsyncMock(),
        )
    assert result["status"] == "committed"
    assert result["success_verdict"]["operator_affirmed"] is True
    submit.assert_called_once()
    friction.assert_called_once()  # the override leaves an honest trail
    assert "operator" in friction.call_args.kwargs["note"]


@pytest.mark.asyncio
async def test_blocked_verdict_with_evidence_paste_still_blocks():
    """Real pasted evidence that shows unfinished work keeps the §17.731 block —
    the claim exemption is narrow, not a hole in the gate."""
    with contextlib.ExitStack() as es:
        friction, submit = _es_patches(es, "incomplete")
        result = await assist_router.assist_submit(
            "sid-1",
            AssistSubmitInput(node_key="T1", action="submit",
                              output="root@pve:~# wget https://x/installer.iso\nsaved 'installer.iso'"),
            AsyncMock(),
        )
    assert result["status"] == "step_incomplete"
    assert result["committed"] is False
    submit.assert_not_called()
    friction.assert_called_once()  # the verify-blocked trail entry
