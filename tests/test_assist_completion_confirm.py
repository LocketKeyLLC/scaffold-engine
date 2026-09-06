"""§17.951 — when the engine can't verify, ASK instead of arguing.

Operator: *"all of that was downloaded. The user stated it was, so set up a
confirmation for this."*

§17.890 already commits on a BARE claim ("it's installed") — the operator's word
outranks a verifier that cannot see their machine. But that gate is deliberately
narrow: it rejects paste-shaped input (that is the evidence path), anything
containing "?", and anything over 280 chars. So the common real shape — evidence
PLUS an assertion, or a long report ending "all of that was downloaded" — took
the evidence path, verified `incomplete`, and the operator was handed another
fix instead of being asked.

Widening §17.890 would be the wrong fix: it would let genuine not-done reports
commit steps. The right fix is to ASK, then honour the answer.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.assist_policy import looks_like_confirmation, looks_like_decline


# ── the affirmative, scoped ───────────────────────────────────────────────


@pytest.mark.parametrize("msg", [
    "confirm", "Confirmed.", "yes", "yeah", "yep", "correct",
    "that's right", "that is correct", "i confirm",
    "it is done", "all done", "finished",
    "mark it complete", "commit it",
])
def test_affirmatives_confirm(msg):
    assert looks_like_confirmation(msg) is True


@pytest.mark.parametrize("msg", [
    # a qualified yes is not a yes
    "yes but the install failed",
    "yes i ran it and got an error",
    # explicit negatives
    "no", "not yet", "it is not done", "that is wrong",
    # ordinary turns must never be read as a confirmation
    "what next?", "here is the output", "",
])
def test_non_affirmatives_do_not_confirm(msg):
    """This detector is loose ON PURPOSE and safe only because it is SCOPED to a
    staged offer. It still must not swallow a qualified yes or a fresh report."""
    assert looks_like_confirmation(msg) is False


@pytest.mark.parametrize("msg", ["no", "nope", "not yet", "not quite",
                                 "hold on", "wait", "that's wrong", "cancel"])
def test_declines_are_recognised(msg):
    assert looks_like_decline(msg) is True


# ── staging and clearing ──────────────────────────────────────────────────


def _db():
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    return db


async def test_stage_writes_a_scoped_offer():
    from app.modules import assist_notes

    db = _db()
    offer = await assist_notes.stage_completion_confirm(
        session_id="s1", node_key="T29", reason="ollama not installed", db=db)
    assert offer["node_key"] == "T29"
    assert offer["reason"] == "ollama not installed"
    assert offer["ts"]
    sql = " ".join(str(db.execute.await_args[0][0]).split())
    assert "pending_completion_confirm" in db.execute.await_args[0][1]["patch"]
    assert "COALESCE(metadata, '{}'::jsonb)" in sql   # merge, never clobber


async def test_read_returns_none_without_a_node_key():
    """A malformed offer must not be actionable."""
    from app.modules import assist_notes

    db = MagicMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {
        "metadata": {"pending_completion_confirm": {"reason": "x"}}}
    db.execute = AsyncMock(return_value=row)
    assert await assist_notes.get_pending_completion_confirm(
        session_id="s1", db=db) is None


async def test_read_fails_soft():
    from app.modules import assist_notes

    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("boom"))
    assert await assist_notes.get_pending_completion_confirm(
        session_id="s1", db=db) is None


async def test_clear_removes_only_that_key():
    from app.modules import assist_notes

    db = _db()
    await assist_notes.clear_pending_completion_confirm(session_id="s1", db=db)
    sql = " ".join(str(db.execute.await_args[0][0]).split())
    assert "- 'pending_completion_confirm'" in sql
    assert "COALESCE(metadata, '{}'::jsonb)" in sql


# ── the turn-loop wiring ──────────────────────────────────────────────────


def test_offer_is_staged_when_a_submit_is_blocked():
    import inspect

    from app.modules import assist_turn

    src = inspect.getsource(assist_turn._run_turn_inner)
    assert "stage_completion_confirm" in src
    assert "reply `confirm`" in src


def test_resolution_runs_before_the_decision_layer():
    """A bare "yes" carries no intent the classifier could route sensibly — the
    ONLY thing that makes it a completion is that the engine just asked. So the
    offer must be resolved before decide, not after."""
    import inspect

    from app.modules import assist_turn

    src = inspect.getsource(assist_turn._run_turn_inner)
    assert src.index("get_pending_completion_confirm") < src.index("decide_turn")


def test_a_superseded_offer_is_cleared():
    """Anything that isn't a yes or a no drops the offer: the operator has moved
    on, and a stale "confirm?" must not attach to a step they have left."""
    import inspect

    from app.modules import assist_turn

    src = inspect.getsource(assist_turn._run_turn_inner)
    assert src.count("_clear_completion_confirm") >= 3   # confirm, decline, else


def test_confirming_commits_and_then_advances():
    import inspect

    from app.modules import assist_turn

    src = inspect.getsource(assist_turn._run_turn_inner)
    i = src.index("looks_like_confirmation")
    tail = src[i:i + 2500]
    assert "assist_submit" in tail
    assert "_claim_and_guide" in tail          # moves on after committing
    assert "completion_confirmed" in tail
