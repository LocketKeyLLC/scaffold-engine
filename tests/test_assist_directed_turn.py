"""§17.903 — the directed-turn contract: no turn ends without direction.

The live failure, in three compounding parts. The operator wrote:

    "i hit the reboot now and its still hung up, perhaps we should start over.
     Delete this VM and start over?"

1. The /decide model classified it `ask`. A deterministic pivot override
   (rule 4) re-routed it to `note` — and the note branch RECORDED and RETURNED.
   The operator's direct question got no answer at all. Log:
   `decide_turn_override ask->note reason=pivot`.
2. Out of that silence they pressed Guide, and the engine served a walkthrough
   opening with `sudo apt update` whose own Prerequisites read "VM 106 must be
   fully installed with Ubuntu 22.04 LTS and reachable via shell" — the exact
   thing they had just reported hung. Nothing represented "blocked".
3. §17.864's premise check, which compares a step against current reality, is
   wired only into the step-claim path, so it never ran (zero premise log lines
   in four hours).

Three fixes, tested here: the override now records AND answers; a blocked
report routes to the blocker instead of the step; and an answerable question
gets a lean rather than a menu.
"""
from __future__ import annotations

import pytest

from app.modules import assist_policy as P

# The verbatim message that produced the incident.
LIVE = ("i hit the reboot now and its still hung up, perhaps we should start "
        "over. Delete this VM and start over?")


# ── 1. blocked detection ─────────────────────────────────────────────────────

@pytest.mark.parametrize("msg", [
    LIVE,
    "in the install it appears to be hung on the 'curtin command in-target'",
    "it's still hung up",
    "still not working",
    "still stuck",
    "the installer is frozen at 66%",
    "stuck on the loading screen",
    "it won't boot",
    "it keeps hanging",
    "the box is not responding",
    "i can't get past the login prompt",
    "nothing happens when i press enter",
])
def test_blocked_positive(msg):
    assert P.looks_like_blocked(msg) is True


@pytest.mark.parametrize("msg", [
    "",
    # Work in flight is a progress report, however slow — not a blocker.
    "still downloading, about 40% done",
    "it is still installing",
    "still running but almost there",
    # A bare error paste is the §17.874 FIX path: the command ran and failed.
    # A blocker is "I cannot get to where your step assumes I already am".
    "root@pve:~# qm start 106",
    "command not found: pct",
    # Completions and ordinary chatter.
    "It worked Ubuntu Server is now downloading!",
    "ok thanks",
    "what is next",
])
def test_blocked_negative(msg):
    assert P.looks_like_blocked(msg) is False


# ── 2. the pivot override records AND answers ────────────────────────────────

def test_pivot_question_is_recorded_and_answered():
    """The regression that started this. The note is still produced — the plan
    impact matters — but it no longer swallows the answer."""
    out = P.apply_deterministic_overrides(
        {"action": "ask", "confidence": "high", "signals": {}}, LIVE)
    assert out["action"] == "note"            # still recorded
    assert out["override"] == "pivot"
    assert out["answer_query"] == LIVE.strip()  # …and now answered


def test_declarative_pivot_without_a_question_is_note_only():
    """A statement of intent needs no answer tail — only questions do, so an
    ordinary pivot must not start paying for a research call."""
    out = P.apply_deterministic_overrides(
        {"action": "ask", "confidence": "high", "signals": {}},
        "lets use docker instead of lxc for this")
    assert out["action"] == "note"
    assert not out.get("answer_query")


def test_completion_claim_still_beats_the_pivot_rule():
    """§17.890 must keep precedence — it is checked before the pivot rule and
    a completion is not a plan change."""
    out = P.apply_deterministic_overrides(
        {"action": "ask", "confidence": "high", "signals": {}},
        "I already did that")
    assert out["action"] == "submit"


def test_orientation_still_wins():
    """§17.867 — a pure "what's next" must stay `status`."""
    out = P.apply_deterministic_overrides(
        {"action": "note", "confidence": "high", "signals": {}}, "whats next??")
    assert out["action"] == "status"


# ── 3. answerable questions deserve a lean ───────────────────────────────────

@pytest.mark.parametrize("msg", [
    LIVE,
    "Delete this VM and start over?",
    "should i do ubuntu server or ubuntu server (minimized)?",
    "should we use docker or lxc?",
    "which one is better?",
    "what do you recommend?",
    "do i need to install docker first?",
    "rebuild the VM?",
])
def test_wants_a_recommendation_positive(msg):
    assert P.wants_a_recommendation(msg) is True


@pytest.mark.parametrize("msg", [
    "",
    # A how-to wants instructions, not a lean — that is the §17.733 ask path.
    "how do i install docker?",
    "what is curtin?",
    # Statements are not questions.
    "it worked",
    "the download is still going",
    "i ran it and it printed nothing",
    "root@pve:~# qm start 106",
])
def test_wants_a_recommendation_negative(msg):
    assert P.wants_a_recommendation(msg) is False


# ── 4. the directive itself ──────────────────────────────────────────────────

def test_recommendation_directive_demands_an_answer_first():
    from app.modules.assist_directives import apply_recommendation
    out = apply_recommendation("BASE")
    assert out.startswith("BASE")
    low = out.lower()
    # The three things that failed the operator: no answer, a menu instead of a
    # lean, and dodging a destructive recommendation.
    assert "first line is the answer" in low
    assert "menu" in low
    assert "destructive" in low


def test_recommendation_directive_is_a_noop_when_disabled():
    from app.modules.assist_directives import apply_recommendation
    assert apply_recommendation("BASE", enabled=False) == "BASE"


def test_ask_path_carries_the_recommendation_rule():
    """The ask path is the one the operator uses to ask a direct question."""
    import inspect
    from app.modules import assist_research_lib
    src = inspect.getsource(assist_research_lib)
    assert "apply_recommendation(" in src


def test_fix_path_carries_it_too():
    """The blocked flow routes through the fix path, and a blocked operator
    asking "should we start over?" needs a lean, not a balanced menu."""
    import inspect
    from app.modules import assist_guide
    src = inspect.getsource(assist_guide.generate_fix)
    assert "apply_recommendation(" in src


# ── 5. the turn loop wiring ──────────────────────────────────────────────────

def test_blocked_branch_precedes_the_decision_layer():
    """Being unable to reach the step at all is the dominant fact of the turn:
    if the decision layer ran first it could route a blocker to `fix` for the
    CURRENT step, which is the wrong step to be fixing."""
    import inspect
    from app.modules import assist_turn
    src = inspect.getsource(assist_turn)
    assert "_blocked_flow" in src
    assert src.index("looks_like_blocked") < src.index("assist_decide.decide_turn")


def test_note_branch_answers_before_returning():
    """The shape that caused the silent dead end: record, return, nothing said."""
    import inspect
    from app.modules import assist_turn
    src = inspect.getsource(assist_turn)
    note_branch = src[src.index('action == "note" or impact == "reshape"'):]
    note_branch = note_branch[:note_branch.index('handled["v"] = "note"')]
    assert "answer_query" in note_branch
    assert "_answer(" in note_branch
