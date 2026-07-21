"""§17.504 — assist-intent nudge.

A free-text message that asks the engine to *assist with implementing an
existing build* (e.g. the real transcript "assist with the completion and
implementation of the defruscio homelab using provided components") is NOT
the `/assist` command — the leading word "assist" is prose, so dispatch
falls through to the triage planner. The user then sees 4-section planning
replies while believing they're in Assist Mode, and no assist session is
ever created.

§17.504 detects that intent and prepends a one-line nudge pointing at the
real `/assist <job_id>` entry point. The triage planning reply still runs
(the nudge is additive).

Pins: the real transcript triggers it; imperative help-requests trigger it;
project *descriptions* that merely mention "assist" do NOT; pipe() emits the
nudge before triage for an assist-intent message and omits it otherwise.
"""
from unittest.mock import patch

import pytest

from tests._scaffold_router_setup import Pipeline


@pytest.fixture
def pipe():
    return Pipeline()


def _drive_pipe(pipe, user_message: str, messages: list[dict]) -> str:
    """Run pipe() with triage stubbed so we don't hit the LLM.

    §17.626 — natural-language assist START runs before the nudge and would
    otherwise make a live `/assist/candidates` call. The nudge is the FALLBACK
    for when no existing job matches, so stub the start to 'no match' (None) to
    exercise that fallback deterministically."""
    with patch.object(pipe, "_call_triage", return_value="TRIAGE_OUTPUT"), \
         patch.object(pipe, "_assist_try_natural_start", return_value=None):
        return "".join(pipe.pipe(user_message, "model-id", messages, {}))


# ---------------------------------------------------------------------------
# Classifier: _looks_like_assist_intent
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestAssistIntentClassifier:
    def test_real_transcript_triggers(self, pipe):
        # The exact message from the DeFruscio HomeLab chat.
        assert pipe._looks_like_assist_intent(
            "assist with the completion and implementation of the "
            "defruscio homelab using provided components."
        )

    @pytest.mark.parametrize("msg", [
        "assist me with finishing my homelab",
        "Please assist with deploying the stack",
        "can you assist with setting up the services",
        "help me implement the remaining pieces",
        "help me complete the deployment",
        "walk me through the next steps",
        "step through the build with me",
    ])
    def test_imperative_help_requests_trigger(self, pipe, msg):
        assert pipe._looks_like_assist_intent(msg), msg

    @pytest.mark.parametrize("msg", [
        # Project *descriptions* that merely mention assist/help — must NOT fire.
        "build an app that assists users to complete forms",
        "I want a chatbot that helps customers finish checkout",
        "design a CLI tool for converting screenshots to PDF",
        "create a homelab dashboard with media and monitoring",
        "what control panels are good for a homelab",
    ])
    def test_descriptions_do_not_trigger(self, pipe, msg):
        assert not pipe._looks_like_assist_intent(msg), msg

    def test_empty_is_false(self, pipe):
        assert not pipe._looks_like_assist_intent("")


# ---------------------------------------------------------------------------
# pipe() integration
# ---------------------------------------------------------------------------


@pytest.mark.smoke
class TestNudgeInPipe:
    def test_assist_intent_message_gets_nudge_then_triage(self, pipe):
        out = _drive_pipe(
            pipe,
            "assist with the completion and implementation of the homelab",
            messages=[{"role": "user",
                       "content": "assist with the completion and "
                                  "implementation of the homelab"}],
        )
        assert "/assist <job_id>" in out, (
            "§17.504: assist-intent message must surface the /assist entry point"
        )
        assert "Assist Mode" in out
        # Triage still runs — nudge is additive, not a redirect.
        assert "TRIAGE_OUTPUT" in out

    def test_normal_planning_message_no_nudge(self, pipe):
        out = _drive_pipe(
            pipe,
            "I want to build a markdown linter",
            messages=[{"role": "user",
                       "content": "I want to build a markdown linter"}],
        )
        assert "/assist <job_id>" not in out
        assert "TRIAGE_OUTPUT" in out
