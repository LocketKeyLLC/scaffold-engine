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


# ── §17.952 — the offer has to SURVIVE, not just stream once ──────────────
#
# Live on 2026-09-06 the offer was staged three times (T29 11:37, T29 11:40,
# T31 12:09) and appeared in ZERO of the session's 584 turns. Staging recorded
# THAT the engine asked; nothing recorded WHAT it asked. The SPA renders the
# streamed bubble into `ephemeralTail` only and rebuilds the transcript from
# `assist_turns` on every reload, so the invitation evaporated — the operator
# read a run of fixes with no sign the engine had ever offered to take their
# word, and forced T29 with the Done button instead.

_OFFER_SID = "99999999-8888-7777-6666-555555555555"


async def _drive_blocked_submit(**extra_patches):
    """Drive a verify-BLOCKED submit through the real turn loop."""
    from app.modules import assist_turn

    submit_res = {"status": "step_incomplete",
                  "success_verdict": {"outcome": "incomplete",
                                      "reason": "no evidence JupyterLab is running"}}
    stack = {
        "app.modules.assist_agent.ingest_turn": AsyncMock(),
        "app.modules.assist_decide.decide_turn": AsyncMock(return_value={
            "action": "submit", "confidence": "high",
            "node_key": "T29", "evidence": "installed it"}),
        "app.routers.assist.assist_submit": AsyncMock(return_value=submit_res),
        "app.modules.assist_agent.run_step_fix": AsyncMock(
            return_value={"fix": "check `systemctl status jupyter`"}),
    }
    stack.update(extra_patches)

    patches = [patch(t, new=m) for t, m in stack.items()]
    for p in patches:
        p.start()
    try:
        out = []
        async for name, data in assist_turn.run_turn(
            session_id=_OFFER_SID, message="all of that was downloaded",
            command="message", node_key="T29", history=[], db=AsyncMock(),
        ):
            out.append((name, data))
        return out
    finally:
        for p in reversed(patches):
            p.stop()


@pytest.mark.asyncio
async def test_offer_is_written_to_the_transcript():
    """The offer is a QUESTION awaiting an answer, so it must be captured like
    every other substantive reply (§17.873) — otherwise it dies on reload."""
    capture = AsyncMock()
    ev = await _drive_blocked_submit(**{
        "app.modules.assist_agent.capture_assistant_reply": capture,
    })

    answers = [d for n, d in ev if n == "assist_answer"]
    assert "reply `confirm`" in answers[0]["text"]        # still streamed, still leads

    offer_captures = [
        c for c in capture.await_args_list
        if "reply `confirm`" in (c.kwargs.get("content") or "")
    ]
    assert offer_captures, "the completion offer was never persisted to assist_turns"
    kw = offer_captures[0].kwargs
    assert kw["kind"] == "ask"
    assert kw["node_key"] == "T29"
    assert kw["session_id"] == _OFFER_SID
    # What is persisted is EXACTLY what was streamed — a transcript that
    # paraphrases the question the operator is answering is worse than none.
    assert kw["content"] == answers[0]["text"]


@pytest.mark.asyncio
async def test_a_failed_stage_records_no_phantom_offer():
    """If staging blows up the offer never reached the operator, so the
    transcript must not claim the engine asked. Capture hangs off `else`."""
    capture = AsyncMock()
    ev = await _drive_blocked_submit(**{
        "app.modules.assist_notes.stage_completion_confirm":
            AsyncMock(side_effect=RuntimeError("db down")),
        "app.modules.assist_agent.capture_assistant_reply": capture,
    })

    assert not [c for c in capture.await_args_list
                if "reply `confirm`" in (c.kwargs.get("content") or "")]
    # and the turn still completes into the §17.884 continuation fix
    assert any("systemctl status jupyter" in d.get("text", "")
               for n, d in ev if n == "assist_answer")
    assert ev[-1][1]["handled"] == "submit"


@pytest.mark.asyncio
async def test_a_capture_failure_never_blocks_the_turn():
    """Persistence is best-effort: a transcript write that fails must not cost
    the operator the offer they can still see, nor the fix underneath it."""
    ev = await _drive_blocked_submit(**{
        "app.modules.assist_agent.capture_assistant_reply":
            AsyncMock(side_effect=RuntimeError("write failed")),
    })

    answers = [d for n, d in ev if n == "assist_answer"]
    assert "reply `confirm`" in answers[0]["text"]
    assert any("systemctl status jupyter" in a["text"] for a in answers)
    assert ev[-1][1]["handled"] == "submit"


# ── §17.953 — the offer has to be readable at the END of the reply too ────


@pytest.mark.asyncio
async def test_offer_is_repeated_after_the_fix():
    """§17.951 leads with the offer because "they read the top of the reply".
    In a bottom-anchored chat the long fix underneath scrolls it off-screen, so
    a one-liner closes the reply where the eye actually lands."""
    ev = await _drive_blocked_submit(**{
        "app.modules.assist_agent.capture_assistant_reply": AsyncMock(),
    })
    answers = [d["text"] for n, d in ev if n == "assist_answer"]

    assert "reply `confirm`" in answers[0]           # still leads
    assert "systemctl status jupyter" in answers[1]  # the §17.884 fix
    assert "confirm" in answers[-1]                  # and closes
    assert answers[-1] is not answers[0]
    # The nudge is a one-liner, not a re-print of the whole offer.
    assert len(answers[-1]) < len(answers[0])


@pytest.mark.asyncio
async def test_the_trailing_nudge_is_persisted_too():
    capture = AsyncMock()
    await _drive_blocked_submit(**{
        "app.modules.assist_agent.capture_assistant_reply": capture,
    })
    contents = [c.kwargs.get("content") or "" for c in capture.await_args_list]
    assert sum(1 for c in contents if "confirm" in c) >= 2, contents


@pytest.mark.asyncio
async def test_no_nudge_when_no_offer_was_made():
    """A staging failure means the operator was never offered anything — the
    reply must not close by inviting them to confirm an offer they never saw."""
    ev = await _drive_blocked_submit(**{
        "app.modules.assist_notes.stage_completion_confirm":
            AsyncMock(side_effect=RuntimeError("db down")),
        "app.modules.assist_agent.capture_assistant_reply": AsyncMock(),
    })
    answers = [d["text"] for n, d in ev if n == "assist_answer"]
    assert answers, "the fix itself must still be emitted"
    assert not any("reply `confirm`" in a for a in answers)


# ── §17.955 — the project-complete announcement is durable ────────────────


@pytest.mark.asyncio
async def test_project_complete_message_is_captured():
    """The most consequential "you are done" the engine emits, and it was
    streamed only — so on reload the transcript ended on the last fix."""
    from app.modules import assist_turn

    capture = AsyncMock()
    with patch("app.modules.assist_agent.get_session",
               new=AsyncMock(return_value={"current_node_key": None,
                                           "status": "active"})), \
         patch("app.routers.assist.assist_next",
               new=AsyncMock(return_value={"node_key": None,
                                           "status": "completed"})), \
         patch("app.modules.assist_agent.capture_assistant_reply", new=capture):
        out = []
        async for name, data in assist_turn._claim_and_guide(
            "77777777-6666-5555-4444-333333333333", None, [], AsyncMock(),
            orient=False,
        ):
            out.append((name, data))

    answers = [d["text"] for n, d in out if n == "assist_answer"]
    assert any("the project is complete" in a for a in answers)
    assert any("the project is complete" in (c.kwargs.get("content") or "")
               for c in capture.await_args_list), "completion message not persisted"
