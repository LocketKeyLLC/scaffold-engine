"""§17.1017 — a hedged completion report routes to submit, not to fix.

§17.1016 made an 'unclear' verdict hold the step open when the operator says
they cannot tell, and §17.1014 answers such a block with the step's own
`## Verify` checks. Driving the live SPA path showed neither could ever fire
there: `POST /assist/{id}/message` with

    "i believe it is done but am unsure."

routed to `action: "fix"` — twice — so the turn never reached the submit path
at all. The operator asked "did this work?" and got a troubleshooting diagnosis
for a problem nobody had reported.

A hedge is the same message SHAPE as a completion claim with the opposite
certainty, so it routes the same way and carries none of the claim's exemption.
"""
import pytest

from app.modules import assist_policy as P
from app.modules.assist_policy import apply_deterministic_overrides as override


def _route(action, msg, **signals):
    out = override({"action": action, "confidence": "high", "signals": signals}, msg)
    return out["action"], out.get("override")


# ── the two readings are exclusive and share their guards ────────────────
@pytest.mark.parametrize("msg", [
    "i believe it is done but am unsure.",       # the live message
    "i think it worked but i am not sure",       # the live message, T35
    "maybe it is installed",
])
def test_a_hedged_report_is_hedged_not_a_claim(msg):
    assert P.hedged_completion_report(msg) is True
    assert P.looks_like_completion_claim(msg) is False


@pytest.mark.parametrize("msg", ["it is done", "it appears to be done",
                                 "I did that already", "it is installed"])
def test_a_confident_report_is_a_claim_not_hedged(msg):
    assert P.looks_like_completion_claim(msg) is True
    assert P.hedged_completion_report(msg) is False


@pytest.mark.parametrize("msg", [
    "i am unsure how to do this",          # help request
    "how do i know if it is done?",        # question
    "it failed and i am unsure why",       # failure report
    "it did not work and i am not sure why",
    "it is not done",
])
def test_neither_reading_claims_the_rest(msg):
    assert P.looks_like_completion_claim(msg) is False
    assert P.hedged_completion_report(msg) is False


def test_uncertainty_wording_is_not_read_as_failure():
    """The negation guard matches bare `not`, so "i am not sure" was read as a
    failure report and the message fell out of the shape entirely — neither a
    claim nor a hedge, so nothing routed it."""
    assert P.hedged_completion_report("i think it worked but i am not sure") is True
    # ...while real failure wording alongside a hedge still disqualifies.
    assert P.hedged_completion_report("it did not work and i am not sure why") is False


# ── the routing override ─────────────────────────────────────────────────
def test_the_live_misroute_is_corrected():
    assert _route("fix", "i believe it is done but am unsure.") == \
        ("submit", "hedged_completion")


@pytest.mark.parametrize("action", ["question", "ask", "note", "status", "advance", "fix"])
def test_every_non_terminal_route_is_overridden(action):
    assert _route(action, "i believe it is done but am unsure.")[0] == "submit"


def test_a_real_shell_error_still_goes_to_fix():
    """Gate 1 returns first on shell_paste+shell_error, so a genuine broken
    command is never swallowed by the hedge gate."""
    act, why = _route("fix", "root@pve:~# pm2 -v\nbash: pm2: command not found",
                      shell_paste=True, shell_error=True)
    assert act == "fix"


def test_help_seeking_is_not_a_completion_report():
    assert _route("fix", "i am unsure how to do this") == ("fix", None)
    assert _route("ask", "how do i check if that worked?")[0] != "submit"


def test_an_already_correct_route_is_left_alone():
    assert _route("submit", "i believe it is done but am unsure.") == ("submit", None)
    assert _route("skip", "i believe it is done but am unsure.") == ("skip", None)


def test_the_override_carries_the_evidence_and_is_observable():
    out = override({"action": "fix", "confidence": "low", "signals": {}},
                   "i believe it is done but am unsure.")
    assert out["evidence"] == "i believe it is done but am unsure."
    assert out["confidence"] == "high", "the caller only dispatches a confident decision"
    assert out["override"] == "hedged_completion"
    assert "deterministic:hedged_completion" in out["rationale"]
