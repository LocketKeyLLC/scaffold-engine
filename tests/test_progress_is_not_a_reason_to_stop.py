"""§17.1381 — a redraft that made PROGRESS is not the end of the chain.

The redraft chain used to split on whether the second draft had fewer refusals
than the first. Fewer → it became the frame and the chain STOPPED. Not fewer →
it went on to a third and a fourth draft. So the drafter was cut off exactly
when it was improving, and the operator got a parked frame with a greyed-out Run
and a refusal the engine knew how to explain.

Measured over every retained log on this deployment — 1,794 `supervised_run_*`
events, 91 redrafts:

    29  → _redraft_clean
    40  → _redraft_rejected   (no progress, so it got a THIRD try)
    22  → straight to park    (progress, so the chain ended)

Seven of those 22 are ADD132 — half of that step's fourteen drafts. The seventh
is the live one that prompted this: the draft had every lesson of §17.1343–1379
right (the password inside `fields`, the body file made in the guest that reads
it, the whole object POSTed to `/test`, `--fail-with-body` on every call) and
carried no Verify section, which is §17.1345's refusal and entirely actionable.
"""
from __future__ import annotations

import inspect
import pathlib

import pytest

from app.modules import execution_agent as ea
from app.modules import supervised_runs as sr
from app.modules.supervised_runs import redraft_again


def _frame(whys: list[str], *, commands=("cmd",), files=()) -> dict:
    return {"refused": [{"command": "c", "why": w} for w in whys],
            "commands": list(commands), "files": list(files)}


#: two refusals whose marker phrases are in _SHAPE_REFUSALS, so a redraft can
#: act on them. These are the live pair from the ADD132 draft that prompted this.
NO_CHECK = ("this block has no check at all: nothing would confirm it")
NO_READBACK = ("this block CHANGES something through the API on port 7878 and "
               "no check reads that API back")
CURL_FAIL = ("this `curl` sends a change but cannot report an HTTP error")


def test_the_markers_used_here_are_really_shape_refusals():
    """Vacuity guard: if these phrases stopped matching `_SHAPE_REFUSALS`, every
    case below would pass for the wrong reason ([[feedback_gate_tests_need_multi_element_input]])."""
    for why in (NO_CHECK, NO_READBACK, CURL_FAIL):
        assert sr.refusal_kinds(_frame([why])), why


# ── the defect this closes ───────────────────────────────────────────────────

def test_progress_still_asks_for_another_draft():
    """THE regression. Live: draft 1 refused for three things including the curl
    shape; draft 2 fixed the curl and was refused for the two check rules. Fewer
    refusals, every one actionable — and the chain stopped."""
    first = _frame([CURL_FAIL, NO_CHECK, NO_READBACK])
    second = _frame([NO_CHECK, NO_READBACK])
    assert len(second["refused"]) < len(first["refused"])
    assert redraft_again(first, second) is True


def test_progress_asks_again_even_when_the_kinds_overlap():
    """The old condition stopped on PARTIAL overlap. Progress overrides that:
    the shared refusal is one the drafter has now been told twice, which is a
    reason to say it more pointedly, not to hand the operator a dead button."""
    first = _frame([CURL_FAIL, NO_CHECK])
    second = _frame([NO_CHECK])
    k1, k2 = sr.refusal_kinds(first), sr.refusal_kinds(second)
    assert k1 & k2 and k2 != k1          # the exact shape that used to stop
    assert redraft_again(first, second) is True


# ── what it must still refuse to do ──────────────────────────────────────────

def test_no_progress_and_partial_overlap_stops():
    """A draft going round in circles: no fewer refusals, and it kept one of the
    old kinds while adding another. That one is bounded, as before."""
    first = _frame([CURL_FAIL, NO_CHECK])
    second = _frame([NO_CHECK, NO_READBACK, CURL_FAIL])
    assert redraft_again(first, second) is False


def test_the_same_kinds_twice_still_gets_one_more():
    """§17.1288e — it followed the step text over the note; say the note outranks
    the text. Unchanged by this fix."""
    first = _frame([NO_CHECK])
    second = _frame([NO_CHECK])
    assert sr.refusal_kinds(first) == sr.refusal_kinds(second)
    assert redraft_again(first, second) is True


def test_disjoint_kinds_still_gets_one_more():
    """It fixed what it was shown and broke something else. Unchanged."""
    assert redraft_again(_frame([CURL_FAIL]), _frame([NO_CHECK])) is True


def test_a_draft_with_nothing_to_show_is_not_redrafted_here():
    assert redraft_again(_frame([CURL_FAIL]), _frame([NO_CHECK], commands=())) is False


def test_a_file_only_draft_counts_as_work():
    """§17.1288k — a written file is work; the no-commands path owns that case."""
    second = _frame([NO_CHECK], commands=(), files=({"path": "/tmp/x.sh", "content": "echo"},))
    assert redraft_again(_frame([CURL_FAIL]), second) is True


def test_a_refusal_no_redraft_can_fix_stops():
    """A permission or denylist refusal is the operator's, not the drafter's:
    `refusal_kinds` is empty and nothing is asked of the model again."""
    second = _frame(["the runner's allow-list does not carry `mkfs`"])
    assert not sr.refusal_kinds(second)
    assert redraft_again(_frame([CURL_FAIL]), second) is False


def test_an_empty_second_draft_stops():
    assert redraft_again(_frame([CURL_FAIL]), {}) is False
    assert redraft_again({}, {}) is False


# ── wired into the chain, and the chain still bounded ────────────────────────

def _chain() -> str:
    src = pathlib.Path(ea.__file__).read_text(encoding="utf-8")
    i = src.index("async def _pause_for_decision(")
    j = src.index("\nasync def ", i + 10)
    return src[i:j]


def test_the_chain_asks_the_helper_rather_than_deciding_inline():
    body = _chain()
    assert "supervised_runs.redraft_again(_first, second)" in body
    # the early exit that ended the chain on progress is gone
    assert 'elif len(second["refused"]) < len(frame["refused"])' not in body


def test_the_better_draft_is_still_what_the_operator_would_see():
    """Continuing must not cost the operator the better draft: `frame` becomes
    the closer one immediately, so a park after a failed third still shows it."""
    body = _chain()
    i = body.index("_closer = bool(second[")
    assert "frame = second" in body[i:i + 1200]
    assert "supervised_run_redraft_closer" in body


def test_the_retry_note_is_told_the_draft_that_was_improved_on():
    """Once `frame` is the better draft, `previous=frame` would hand the note the
    draft as its own predecessor. The first draft is kept by name."""
    body = _chain()
    assert "_first = frame" in body
    assert "shape_retry_note(second, previous=_first" in body
    assert "shape_retry_note(second, previous=frame" not in body


def test_the_chain_is_still_bounded_at_four_drafts():
    """Progress is a reason to continue, not a reason to loop."""
    body = _chain()
    # Read, not guessed: the eight calls are the first draft, the three shape
    # redrafts (fix / fix2 / fix3) and four single-shot recovery drafts.
    assert body.count("supervised_runs.draft_runbook(") == 8
    for note in ("retry_note=fix,", "retry_note=fix2,", "retry_note=fix3,"):
        assert note in body.replace("\n", " ").replace("  ", " ") or note[:-1] in body, note
    assert "retry_note=fix3" in body and "retry_note=fix4" not in body, (
        "progress is a reason to continue, not a reason to loop: the shape chain "
        "stays bounded at four drafts")


def test_the_closer_branch_logs_what_it_decided():
    """§17.1277 — the reasons a redraft was refused are otherwise invisible, and
    this branch used to log nothing at all before parking."""
    body = _chain()
    i = body.index("supervised_run_redraft_closer")
    assert 'refusals=%s' in body[i:i + 400]


@pytest.mark.parametrize("name", ["redraft_again"])
def test_the_helper_is_pure(name):
    """It takes two frames and returns a bool: no DB, no model, no clock — so
    the decision that cost seven operator cycles is testable in isolation."""
    src = inspect.getsource(getattr(sr, name))
    for forbidden in ("await ", "async ", "session", "logger", "datetime", "random"):
        assert forbidden not in src, forbidden
