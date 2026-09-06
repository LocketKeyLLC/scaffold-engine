"""§17.950 — the engine would not move on when the operator said they were done.

Two paths were broken, and the operator hit both:

1. **A pasted/typed acknowledgement that the step is finished did nothing.**
   §17.891 requires a deterministic advancement signal in the operator's own
   words before the tracker may retire a step — a good rule, added because an
   LLM verdict alone once jumped the operator past work they never did. But the
   gate missed the single most natural phrasing:

       "i am connected via SSH! whats next?"     (live, 2026-09-06 01:16:59)

   `looks_like_completion_claim` bails on any "?" (correct — a question is not a
   claim) and `_ADVANCE_INTENT_RE` is anchored `^…$` (so a PREFIXED next-request
   cannot match). A message that both claims completion AND asks to advance
   scored False on both, §17.891 vetoed the verdict, and the operator stayed put.

2. **"Guide me" had no notion of a finished step.** It went straight to
   claim-and-guide, so pressing it after finishing returned the same
   walkthrough, forever.

The fix keeps §17.891 intact: an advancement signal is a VETO layered on top of
`verdict == "advance" AND confident AND current_step_done`. Widening it restores
the tracker's vote; it can never advance on its own.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.assist_policy import has_advancement_signal


# ── the signal the operator actually sent ─────────────────────────────────


def test_the_live_message_now_signals():
    """THE regression. Claims completion and asks to move on, in one breath."""
    assert has_advancement_signal("i am connected via SSH! whats next?") is True


@pytest.mark.parametrize("msg", [
    "whats next?",
    "what's next",
    "what is next?",
    "that is done, what now?",
    "it worked, what is next?",
    "ok on to the next",
    "next step please",
    "where to next?",
    # §17.950 — the `X with that` construction listed only "done"
    "finished with that",
    "complete with this one",
    # unchanged §17.890/891 behaviour
    "the drivers are installed and working",
    "next",
    "continue",
])
def test_advancement_phrasings_signal(msg):
    assert has_advancement_signal(msg) is True


@pytest.mark.parametrize("msg", [
    # asking for the next COMMAND is not asking for the next STEP
    "what is the next command i should run?",
    "what next command do i run",
    "how do i know what to do next?",
    # a failure report is not a request to move on, however it is phrased
    "it failed, what next command should i try",
    "it broke, what now?",
    "the install errored out",
    "i am stuck",
    # ordinary conversation
    "can we switch to ssh so i can copy and paste?",
    "the error is too long to type",
    "",
])
def test_non_advancement_stays_silent(msg):
    """Over-firing is the §17.891 incident itself — a step retired off words
    that never claimed anything. Both directions matter."""
    assert has_advancement_signal(msg) is False


def test_a_failure_report_cannot_signal_via_a_next_request():
    """The disqualifier that guards completion claims guards this class too:
    'it failed, what next…' contains a next-request AND a failure."""
    assert has_advancement_signal("it failed, what next command should i try") is False
    assert has_advancement_signal("that broke, what now?") is False


# ── "Guide me" now reuses the same gates ──────────────────────────────────


def _db_with_last_turn(content):
    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = (
        {"content": content} if content is not None else None)
    db.execute = AsyncMock(return_value=row)
    return db


async def test_guide_picks_up_an_advancement_signal():
    from app.modules.assist_turn import _recent_advance_message

    db = _db_with_last_turn("i am connected via SSH! whats next?")
    out = await _recent_advance_message("s1", "T29", db)
    assert out == "i am connected via SSH! whats next?"


async def test_guide_ignores_a_message_with_no_signal():
    """A Guide press is not itself evidence of anything. With no qualifying
    message behind it, Guide me must re-guide exactly as it always has."""
    from app.modules.assist_turn import _recent_advance_message

    db = _db_with_last_turn("can we switch to ssh so i can copy and paste?")
    assert await _recent_advance_message("s1", "T29", db) is None


async def test_guide_needs_a_step():
    from app.modules.assist_turn import _recent_advance_message

    assert await _recent_advance_message("s1", None, _db_with_last_turn("done")) is None


async def test_guide_probe_reads_only_the_latest_operator_turn():
    """An advancement signal from earlier in a long troubleshooting thread has
    been superseded by whatever came after it."""
    from app.modules.assist_turn import _recent_advance_message

    db = _db_with_last_turn("done")
    await _recent_advance_message("s1", "T29", db)
    sql = " ".join(str(db.execute.await_args[0][0]).split())
    assert "role = 'operator'" in sql
    assert "kind IN ('message', 'submit')" in sql      # never a note
    assert "ORDER BY created_at DESC, id DESC LIMIT 1" in sql


async def test_guide_probe_fails_soft():
    from app.modules.assist_turn import _recent_advance_message

    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("boom"))
    assert await _recent_advance_message("s1", "T29", db) is None


def test_guide_branch_is_wired_to_the_same_tracker_path():
    """It must go through `_track_then_continue` — the SAME machinery the typed
    advance uses — so it inherits §17.891's veto rather than a weaker copy."""
    import inspect

    from app.modules import assist_turn

    src = inspect.getsource(assist_turn._run_turn_inner)
    assert "_recent_advance_message" in src
    assert "_track_then_continue" in src
