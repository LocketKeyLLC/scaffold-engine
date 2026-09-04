"""§17.927 — a conclusion that is not an action leaves the operator parked.

Live (session 613dd1df, turn 1497). The ENTIRE walkthrough for ADD3 was:

    "The operator has explicitly requested to remove this step from the project
     plan. No further action is required for the Markdown linter implementation."

The assessment was right — ADD3/ADD4 are junk steps created from casual test
messages ("I want to build a markdown linter") and really are obsolete — and
nothing was done with it. No skip, no advance, no offer. The operator, who had
just finished ADD5, got a two-sentence dead end: "this appears to be its ability
to move on after completing the task".

§17.915 already draws the distinction this needs: SKIP is "work deliberately NOT
done", the right terminal state for an obsolete step, and unlike a commit it
needs no evidence.
"""
from __future__ import annotations

import pytest

from app.modules.assist_guide import concludes_no_action_required, no_action_footer

pytestmark = pytest.mark.asyncio

LIVE_1497 = ("The operator has explicitly requested to remove this step from the "
             "project plan. No further action is required for the Markdown "
             "linter implementation.")


def test_detects_the_live_dead_end():
    assert concludes_no_action_required(LIVE_1497)


@pytest.mark.parametrize("text", [
    "Nothing further to do here — the service is already running.",
    "This step is no longer required.",
    "No additional action is needed.",
    "This step can be skipped.",
])
def test_detects_other_no_action_conclusions(text):
    assert concludes_no_action_required(text)


def test_a_real_walkthrough_is_not_a_dead_end():
    """Scoped to SHORT replies: a long walkthrough that merely mentions in
    passing that something is already done is still a walkthrough."""
    long_guide = ("## Do this next\n```bash\nqm config 106\n```\n"
                  "The disk is already done being formatted, now install the OS.\n"
                  + "step detail. " * 200)
    assert not concludes_no_action_required(long_guide)


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_is_not_a_conclusion(text):
    assert not concludes_no_action_required(text)


def test_offer_names_the_step_and_the_next_one():
    f = no_action_footer({"node_key": "T23", "title": "Install PalWorld server"},
                         "Implement a Markdown linter")
    assert "`skip`" in f
    assert "Implement a Markdown linter" in f
    assert "T23" in f and "Install PalWorld server" in f


def test_offer_distinguishes_skip_from_done():
    """§17.915 — skipping records work deliberately NOT done. The offer must say
    so, or it becomes a way to mark a step complete without evidence."""
    f = no_action_footer(None, "Some step")
    assert "does NOT claim the work happened" in f
    assert "deliberately not done" in f


def test_offer_survives_no_next_step():
    f = no_action_footer(None, "Last step")
    assert "`skip`" in f and "Last step" in f


def test_wired_into_ensure_guidance_and_fail_soft():
    import inspect
    from app.modules import assist_guide
    src = inspect.getsource(assist_guide.ensure_guidance)
    assert "concludes_no_action_required" in src
    assert "_next_claimable_step" in src
    # the offer must never break a guide
    assert "assist_no_action_offer_failed" in src


def test_next_claimable_excludes_blocked_and_terminal_steps():
    """A step whose dependencies are unmet is not claimable, and a done/skipped
    one is not a destination."""
    import inspect
    from app.modules import assist_guide
    src = inspect.getsource(assist_guide._next_claimable_step)
    assert "n.status NOT IN ('done', 'skipped', 'failed')" in src
    assert "p.status NOT IN ('done', 'skipped')" in src
    assert "ORDER BY n.execution_order" in src
