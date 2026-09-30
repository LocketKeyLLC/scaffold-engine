"""§17.1236 — a step told not to ask must not answer with a question.

Fixtures are the REAL ADD100 step description and the REAL output it produced,
captured from the live job (`tests/fixtures/add100_*.txt`) — not invented text,
because the whole point is that the gate has to bite on what a model actually
writes when it defers (feedback: never verify with synthetic input).

ADD100's description carried the operator's corrections verbatim: they are not
expected to know the technology, the outside-access deferral "is now closed",
pick the approach and say in plain words what it means. What came back was
"## What the tech choice decides — why I am asking", "I cannot build the panel
until this is chosen", and three options to choose between.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.modules import step_constraints as sc

FIX = Path(__file__).parent / "fixtures"
STEP = (FIX / "add100_step.txt").read_text()
OUTPUT = (FIX / "add100_output.txt").read_text()


def test_it_fires_on_the_real_pair():
    v = sc.violation(STEP, OUTPUT)
    assert v, "the live ADD100 pair must be caught"
    assert "not to put the choice back to the operator" in v
    assert "decision node, not in place of this step's work" in v


def test_it_quotes_an_actual_instruction_not_a_passing_word():
    """The first draft matched bare `never ask` inside the step's opening
    sentence "nothing it was never asked for" and quoted that back as the rule
    the output had broken. A gate that names the wrong rule is worse than none."""
    told = sc.forbids_asking(STEP)
    assert told
    assert "never asked for" not in told
    assert told in ("The previous attempt put outside access out of scope and deferred it; "
                    "that deferral is now closed.",) or "not expected to know the technology" in told \
        or "Do not ask" in told or "Pick the approach" in told, told


def test_the_phrase_that_caused_the_false_match_is_not_an_instruction():
    assert sc.forbids_asking(
        "Rework it so it does the things chosen in ADD99 and nothing it was never asked for.") is None
    assert sc.forbids_asking("Add the indexers the operator asked for.") is None
    assert sc.forbids_asking("This is what they asked for, nothing more.") is None


def test_it_names_where_the_output_asked():
    asked = sc.asks_the_operator(OUTPUT)
    assert asked and "why I am asking" in asked


# ── the two-sidedness, which is what keeps it safe ───────────────────────


def test_a_step_that_never_forbade_asking_is_left_alone():
    """A `decision` node's whole job is to ask. It must be impossible to trip."""
    assert sc.violation("Decide: what should the control panel do for you?", OUTPUT) is None
    assert sc.violation("Ask the operator which indexers they want.", OUTPUT) is None
    assert sc.violation("", OUTPUT) is None


def test_a_step_that_forbids_asking_and_delivers_passes():
    delivered = (
        "## Control Panel Rebuild — Implementation\n\n"
        "**Chosen stack:** FastAPI backend, plain HTML/JS frontend, SQLite for state.\n\n"
        "Outside access is served through a reverse proxy with a password, which means you type "
        "one address into your phone and log in once.\n\n"
        "## Files written\n\n`/opt/panel/app.py` — the backend.\n")
    assert sc.violation(STEP, delivered) is None


def test_both_halves_are_required():
    forbidding = "They are not expected to know the technology. Do not ask them to supply a spec."
    asking = "## The question: how should this be built?\n\nPlease choose one."
    assert sc.violation(forbidding, asking)
    assert sc.violation(forbidding, "Built it. Chose FastAPI because it can write the settings.") is None
    assert sc.violation("Build the thing.", asking) is None


# ── what a model actually writes when it defers ──────────────────────────


@pytest.mark.parametrize("deferral", [
    "I cannot build the panel until this is chosen.",
    "## The question: how should the panel be built?",
    "That is why I am asking now instead of guessing.",
    "Which would you prefer?",
    "Please choose one of the three options below.",
    "Let me know which you want and I will build it.",
    "The rebuild comes immediately after your choice.",
    "Outside access is out of scope for this step.",
    "It will be added only after you decide between a proxy and a VPN.",
    "Once you decide, I will wire it up.",
])
def test_the_deferrals_are_recognised(deferral):
    assert sc.asks_the_operator(deferral), deferral


@pytest.mark.parametrize("fine", [
    "",
    "Installed Prowlarr and added five indexers.",
    "The question of which indexer is fastest does not matter here.",
    "I chose FastAPI. In plain terms: one small program serves the page and talks to Proxmox.",
    "You will be able to choose a movie from the panel once this is running.",
    "This step is done; the next step covers Jellyfin.",
])
def test_ordinary_delivery_is_not_an_ask(fine):
    assert sc.asks_the_operator(fine) is None, fine


def test_a_mention_deep_in_a_long_deliverable_does_not_count():
    """Same rule as §17.1235: the refusal to do the work belongs at the top. A
    real deliverable that mentions a later choice on page three is not deferring."""
    long_out = "\n\n".join(["Wrote the backend."] * 12 + ["Once you decide on themes, I can restyle it."])
    assert sc.asks_the_operator(long_out) is None


# ── it is wired where it can act ─────────────────────────────────────────


def test_the_executor_checks_it_before_the_verifiers():
    import inspect
    from app.modules import execution_agent as ea
    src = inspect.getsource(ea.execute_next_node)
    assert "_constraint_violation(" in src
    assert src.index("_constraint_violation(") < src.index("elif skip_verify:")
    assert "node_asked_when_told_not_to" in src
    # the step's OWN text is what it is judged against, all three fields
    seg = src[src.index("_constraint_violation("):src.index("elif skip_verify:")]
    for field in ("description", "prompt_template", "title"):
        assert field in seg, field


# ── §17.1236b: the second draw shared no wording with the first ───────────

OUTPUT2 = (FIX / "add100_output2.txt").read_text()


def test_the_second_live_draw_is_caught_too():
    """Retried, ADD100 asked again in completely different words: "## Decision
    needed: Should the control panel be reachable from outside your home?" and
    "The one thing you did not say is whether…". None of the first draw's
    phrases appear in it."""
    assert not any(p in OUTPUT2 for p in ("why I am asking", "I cannot build", "## The question"))
    assert sc.violation(STEP, OUTPUT2)


def test_the_second_draw_gets_the_more_specific_reason():
    """It is not merely asking when told not to — it re-asks a question the
    operator ANSWERED, and the reason must say so and quote both sides."""
    v = sc.violation(STEP, OUTPUT2)
    assert "ALREADY ANSWERED" in v
    assert "asked and answered" in v                       # the step's own sentence
    assert "reachable from outside your home" in v         # the output's
    assert "Re-asking a settled question" in v


def test_reopens_settled_needs_both_sides():
    settled = 'They were asked and answered: "yes it should be accessible outside".'
    reopen = "You did not say whether you want outside access."
    assert sc.reopens_settled(settled, reopen)
    assert sc.reopens_settled("Build the panel.", reopen) is None
    assert sc.reopens_settled(settled, "Built it, reachable from outside via Tailscale.") is None


def test_unrelated_subjects_are_never_paired():
    """A false positive fails work that was actually done, so the overlap has to
    be about the same thing."""
    assert sc.reopens_settled("The storage pool is already decided: local-lvm.",
                              "You did not say which movie you want first.") is None
    assert sc.reopens_settled("The VLAN id is already settled.",
                              "You have not decided on the wallpaper.") is None


def test_one_distinctive_long_word_is_enough_but_a_short_one_is_not():
    """Measured on the real pair: the two sentences share exactly ONE word,
    `outside`, and are unmistakably the same subject. Requiring two missed the
    case the gate was written for."""
    assert sc.reopens_settled("Remote access is already decided.",
                              "You did not say whether you want remote access.")
    # a single short shared word is not a subject
    assert sc.reopens_settled("The plan is already settled.",
                              "You did not say which plan file to use.") is None


@pytest.mark.parametrize("phrase", [
    "## Decision needed: should this be public?",
    "The one thing you did not say is whether you want it.",
    "You haven't told me which option you prefer.",
    "Here are the options, in plain words:",
    "Before I can build this, I need to know the stack.",
    "Awaiting your decision on the proxy.",
])
def test_the_second_draws_vocabulary_is_recognised(phrase):
    assert sc.asks_the_operator(phrase), phrase


def test_a_delivered_step_that_mentions_a_settled_fact_still_passes():
    """The vacuity check for the new branch: quoting the settled decision while
    DELIVERING must not be read as re-opening it."""
    delivered = (
        "## Control panel rebuilt\n\n"
        "Outside access was already answered — yes — so it is served over Tailscale "
        "with a login page. You open one address on your phone.\n\n"
        "## Files written\n\n`/opt/panel/app.py`\n")
    assert sc.violation(STEP, delivered) is None
