"""§17.868 — the server-side assist turn loop.

One night of live operator testing (§17.861–867) proved a structural fact:
composing the conversational loop CLIENT-side — capture, gates, decide,
dispatch, claim, premise check, guidance, each a separate HTTP call sequenced
by browser JavaScript with shared abort state — fails at every seam. An
impatient click kills an invisible in-flight chain; a reload orphans a
result; each seam failure looks like "the assistant stopped working".

This module moves the WHOLE loop server-side. ``run_turn`` is an async
generator: the client opens one stream per operator turn and renders events;
it never sequences anything. Every stage yields a status frame first, so
there is no silent phase, and state mutations (capture, notes, submits,
claims) commit inside their own stages — a client disconnect stops the
*display*, not the state machine's already-completed work.

Reuses the per-action building blocks exactly as the HTTP endpoints compose
them — the endpoint functions themselves are called where they are clean to
call, so behavior cannot drift from the documented per-verb surfaces.
"""
from __future__ import annotations

import logging
from typing import AsyncIterator

from app.modules import assist_policy
from app.sse_events import (
    ASSIST_ANSWER,
    ASSIST_GUIDE_DELTA,
    ASSIST_GUIDE_DONE,
    ASSIST_NOTE_RECORDED,
    ASSIST_REPLAN_PROPOSAL,
    ASSIST_STEP_OUTCOME,
    ASSIST_TURN_DONE,
    ASSIST_TURN_ROUTED,
    ASSIST_TURN_STATUS,
)

logger = logging.getLogger("scaffold.assist")

_Event = tuple[str, dict]


def _ev(name: str, data: dict) -> _Event:
    return (name, data)


async def run_turn(
    *, session_id: str, message: str | None, command: str,
    node_key: str | None, history: list[dict], db,
) -> AsyncIterator[_Event]:
    """Drive one operator turn end-to-end, yielding (event_name, data) frames.

    ``command='guide'`` skips capture/decide and goes straight to
    claim-and-guide (the Guide / Re-show buttons). ``command='message'``
    runs the full loop on ``message``. The terminal ASSIST_TURN_DONE frame is
    emitted OUTSIDE the work generator — yielding from a ``finally`` raises
    RuntimeError when a disconnected client closes the generator
    (GeneratorExit), which would mask the real teardown.
    """
    handled = {"v": "none"}
    async for e in _run_turn_inner(
        session_id=session_id, message=message, command=command,
        node_key=node_key, history=history, db=db, handled=handled,
    ):
        yield e
    yield _ev(ASSIST_TURN_DONE, {"handled": handled["v"]})


async def _run_turn_inner(
    *, session_id: str, message: str | None, command: str,
    node_key: str | None, history: list[dict], db, handled: dict,
) -> AsyncIterator[_Event]:
    from app.modules import assist_agent

    if True:  # single indent block — keeps the dispatch ladder's early returns flat
        if command == "guide":
            async for e in _claim_and_guide(session_id, node_key, history, db,
                                            orient=False):
                yield e
            handled["v"] = "guide"
            return

        text_ = (message or "").strip()
        if not text_:
            handled["v"] = "empty"
            return  # the outer generator emits the single TURN_DONE frame

        # 1. Unconditional capture (§17.710a) — fail-soft, never blocks.
        yield _ev(ASSIST_TURN_STATUS, {"text": "Reading that…"})
        try:
            await assist_agent.ingest_turn(
                session_id=session_id, role="operator", kind="message",
                content=text_, node_key=node_key, db=db,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("turn_loop_capture_failed sid=%s err=%r", session_id, exc)

        # 2. Deterministic orientation (§17.867) — zero model calls.
        if assist_policy.looks_like_whats_next(text_):
            yield _ev(ASSIST_TURN_ROUTED, {"action": "status", "override": "whats_next"})
            async for e in _claim_and_guide(session_id, node_key, history, db,
                                            orient=True):
                yield e
            handled["v"] = "status"
            return

        # 3. The unified decision (§17.771 + §17.855 overrides run inside).
        yield _ev(ASSIST_TURN_STATUS, {"text": "Deciding how to act on that…"})
        d: dict = {}
        try:
            from app.modules import assist_decide
            d = await assist_decide.decide_turn(
                session_id=session_id, message=text_, node_key=node_key,
                history=history, db=db,
            ) or {}
        except Exception as exc:  # noqa: BLE001 — decide down → fallback below
            logger.warning("turn_loop_decide_failed sid=%s err=%r", session_id, exc)
        action = (d.get("action") or "").strip()
        confident = (d.get("confidence") or "low") != "low"
        yield _ev(ASSIST_TURN_ROUTED, {
            "action": action or "fallback",
            "override": d.get("override"),
        })
        impact = (d.get("plan_impact") or "none").strip()
        nk = (str(d.get("node_key") or "").strip() or node_key)

        # 4. Dispatch — mirrors the pipeline's `_dispatch_decision` semantics.
        if confident and (action == "note" or impact == "reshape"):
            async for e in _note(session_id, d, text_, nk, db):
                yield e
            handled["v"] = "note"
            return
        if confident and action == "submit":
            done = False
            async for e in _submit(session_id, d, text_, nk, history, db):
                if e[0] == ASSIST_STEP_OUTCOME and e[1].get("status") == "committed":
                    done = True
                yield e
            if done:
                async for e in _claim_and_guide(session_id, None, history, db,
                                                orient=False):
                    yield e
            handled["v"] = "submit"
            return
        if confident and action == "fix":
            yield _ev(ASSIST_TURN_STATUS, {"text": "Diagnosing the error for your environment…"})
            fix = await assist_agent.run_step_fix(
                session_id=session_id, node_key=nk,
                error=(d.get("error_text") or text_), history=history, db=db,
            )
            yield _ev(ASSIST_ANSWER, {"kind": "fix", "text": (fix or {}).get("fix") or "(no fix returned)"})
            if impact == "surface":
                async for e in _surface(session_id, d, text_, nk, db):
                    yield e
            handled["v"] = "fix"
            return
        if confident and action in ("ask", "question"):
            yield _ev(ASSIST_TURN_STATUS, {"text": "Researching your question against the project's current state — this can take a minute or two…"})
            res = await assist_agent.run_step_research(
                session_id=session_id, node_key=nk,
                question=(d.get("query") or text_), history=history, db=db,
            )
            answer = (res or {}).get("answer") or ""
            if answer.strip():
                yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": answer})
            else:
                yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": "I couldn't put together a useful answer for that — try rephrasing, or ask me to guide the current step."})
            if impact == "surface":
                async for e in _surface(session_id, d, text_, nk, db):
                    yield e
            handled["v"] = "ask"
            return
        if confident and action == "status":
            async for e in _claim_and_guide(session_id, nk, history, db, orient=True):
                yield e
            handled["v"] = "status"
            return

        # 5. Fallback (low confidence / unhandled action): the progress
        # tracker, then guidance — the pre-§17.771 default, server-side.
        yield _ev(ASSIST_TURN_STATUS, {"text": "Checking step progress…"})
        advanced = False
        try:
            from app.routers.assist import assist_track
            from app.routers.assist import AssistTrackInput
            tr = await assist_track(
                session_id,
                AssistTrackInput(message=text_, node_key=nk, history=history),
                db=db,
            )
            if (tr or {}).get("action") == "advanced":
                yield _ev(ASSIST_STEP_OUTCOME, {
                    "node_key": (tr or {}).get("retired_prior_step") or nk,
                    "status": "committed",
                })
                advanced = True
        except Exception as exc:  # noqa: BLE001
            logger.warning("turn_loop_track_failed sid=%s err=%r", session_id, exc)
        async for e in _claim_and_guide(session_id, None if advanced else nk,
                                        history, db, orient=False):
            yield e
        handled["v"] = "fallback"


async def _note(session_id: str, d: dict, text_: str, nk, db) -> AsyncIterator[_Event]:
    yield _ev(ASSIST_TURN_STATUS, {"text": "Recording that and checking whether it changes the plan…"})
    from app.routers.assist import AssistNoteInput, assist_note
    kind = d.get("note_kind") or ("decision" if (d.get("plan_impact") == "reshape") else "note")
    res = await assist_note(
        session_id,
        AssistNoteInput(text=(d.get("note_text") or text_), kind=kind, node_key=nk),
        db=db,
    )
    yield _ev(ASSIST_NOTE_RECORDED, {
        "kind": kind,
        "retracted": len(res.get("retracted_facts") or []),
        "has_proposal": bool(res.get("replan_proposal")),
    })
    if res.get("replan_proposal"):
        yield _ev(ASSIST_REPLAN_PROPOSAL, {"proposal": res["replan_proposal"]})


async def _surface(session_id: str, d: dict, text_: str, nk, db) -> AsyncIterator[_Event]:
    """plan_impact=surface, actionable (§17.863): record + impact pass; only a
    concrete proposal surfaces — silence stays silent."""
    try:
        async for e in _note(session_id, d, text_, nk, db):
            if e[0] != ASSIST_TURN_STATUS:  # keep surface quiet unless material
                yield e
    except Exception as exc:  # noqa: BLE001
        logger.warning("turn_loop_surface_failed sid=%s err=%r", session_id, exc)


async def _submit(session_id: str, d: dict, text_: str, nk, history, db) -> AsyncIterator[_Event]:
    yield _ev(ASSIST_TURN_STATUS, {"text": "Recording the result and verifying the step…"})
    from app.routers.assist import AssistSubmitInput, assist_submit
    try:
        res = await assist_submit(
            session_id,
            AssistSubmitInput(node_key=nk, output=(d.get("evidence") or text_),
                              action="submit", history=history),
            db=db,
        )
        yield _ev(ASSIST_STEP_OUTCOME, {
            "node_key": nk, "status": (res or {}).get("status") or "recorded",
        })
    except Exception as exc:  # noqa: BLE001 — a refused submit must not kill the turn
        yield _ev(ASSIST_TURN_STATUS, {
            "text": f"That looked like a step result, but the step wouldn't accept it ({exc}). Continuing…",
        })


async def _claim_and_guide(
    session_id: str, node_key, history, db, *, orient: bool,
) -> AsyncIterator[_Event]:
    """Claim (when needed) → premise-verify → stream the walkthrough. The one
    sequence whose client-side composition caused every 'stuck' report."""
    from app.modules import assist_agent
    from app.routers.assist import assist_next

    sess = await assist_agent.get_session(session_id=session_id, db=db)
    nk = node_key or (sess or {}).get("current_node_key")
    if not nk:
        yield _ev(ASSIST_TURN_STATUS, {"text": "Finding the next step and verifying it against what we know…"})
        nxt = await assist_next(session_id, db=db)
        nk = (nxt or {}).get("node_key")
        pc = (nxt or {}).get("premise_check") or {}
        if pc.get("stale"):
            yield _ev(ASSIST_TURN_STATUS, {"text": f"⚠ Before we walk into step {nk}: {pc.get('reason') or 'its premise may be out of date.'}"})
        if not nk:
            yield _ev(ASSIST_TURN_STATUS, {"text": "No claimable step right now — the session may be waiting on a decision or already complete."})
            return
    if orient:
        counts = (sess or {}).get("step_counts") or {}
        done = counts.get("committed") or 0
        total = sum(v for v in counts.values() if isinstance(v, int))
        yield _ev(ASSIST_TURN_STATUS, {
            "text": f"📍 You're on step {nk}" + (f" ({done}/{total} done)" if total else "") + ". Here's the current walkthrough:",
        })
    yield _ev(ASSIST_TURN_STATUS, {"text": f"Preparing the walkthrough for {nk}…"})
    async for ev in assist_agent.generate_step_guidance_stream(
        session_id=session_id, node_key=nk, history=history, db=db,
    ):
        if ev.get("type") == "delta":
            yield _ev(ASSIST_GUIDE_DELTA, {"text": ev.get("text") or ""})
        else:
            yield _ev(ASSIST_GUIDE_DONE, {
                "status": ev.get("status"), "node_key": nk,
                "guidance_meta": ev.get("guidance_meta") or {},
                "cached": ev.get("cached", False),
            })
