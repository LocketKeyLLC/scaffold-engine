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


# ── §17.869 — detached turn runs ─────────────────────────────────────────────
# The §17.868 loop still died with the browser: a multi-minute turn (decide →
# verify → premise → guide) was killed mid-flight when the operator reloaded
# during a slow stage — the disconnect watch cancelled the generator, and the
# remaining stages silently never ran (the live 02:09 incident). The loop now
# runs as a BACKGROUND task appending every frame to ``assist_turn_runs``;
# clients tail the row from frame 0, so a reload replays what was missed and
# resumes live. The turn ALWAYS completes server-side. (Same detachment the
# research agent got in §17.454/820.)

import asyncio
import json as _json

from sqlalchemy import text as _sqltext


async def start_turn_run(
    *, session_id: str, message: str | None, command: str,
    node_key: str | None, history: list[dict],
) -> str:
    """Create the durable run row and spawn the background driver. Returns the
    run id immediately — the caller tails it."""
    from app.database import async_session

    async with async_session() as db:
        run_id = (await db.execute(
            _sqltext("INSERT INTO assist_turn_runs (session_id) VALUES (:sid) RETURNING id"),
            {"sid": session_id},
        )).scalar()
        await db.commit()
    run_id = str(run_id)
    asyncio.create_task(_drive_turn_run(
        run_id=run_id, session_id=session_id, message=message,
        command=command, node_key=node_key, history=history,
    ))
    return run_id


async def _append_frames(run_id: str, frames: list[_Event], db) -> None:
    payload = _json.dumps([{"e": n, "d": d} for n, d in frames])
    await db.execute(
        _sqltext("UPDATE assist_turn_runs SET frames = frames || CAST(:f AS jsonb) WHERE id = :rid"),
        {"rid": run_id, "f": payload},
    )
    await db.commit()


async def _drive_turn_run(
    *, run_id: str, session_id: str, message: str | None, command: str,
    node_key: str | None, history: list[dict],
) -> None:
    """Run the loop to completion on its OWN session (§17.621 pattern),
    appending frames as they happen. Guide deltas are coalesced (~0.7s) so a
    long walkthrough doesn't hammer the row with per-token commits."""
    from app.database import async_session

    status = "done"
    try:
        async with async_session() as db:
            buf: list[_Event] = []
            last_flush = asyncio.get_event_loop().time()
            async for ev in run_turn(
                session_id=session_id, message=message, command=command,
                node_key=node_key, history=history, db=db,
            ):
                buf.append(ev)
                now = asyncio.get_event_loop().time()
                if ev[0] != ASSIST_GUIDE_DELTA or (now - last_flush) >= 0.7:
                    await _append_frames(run_id, buf, db)
                    buf, last_flush = [], now
            if buf:
                await _append_frames(run_id, buf, db)
    except Exception as exc:  # noqa: BLE001 — the run row carries the error
        status = "error"
        logger.exception("turn_run_failed run_id=%s", run_id)
        try:
            async with async_session() as db:
                await _append_frames(run_id, [("error", {"detail": str(exc)})], db)
        except Exception:  # noqa: BLE001
            pass
    finally:
        try:
            async with async_session() as db:
                await db.execute(
                    _sqltext("UPDATE assist_turn_runs SET status = :st, finished_at = now() WHERE id = :rid"),
                    {"rid": run_id, "st": status},
                )
                await db.commit()
        except Exception:  # noqa: BLE001
            logger.exception("turn_run_finalize_failed run_id=%s", run_id)


async def tail_turn_run(run_id: str) -> AsyncIterator[_Event]:
    """Yield the run's frames from the beginning, then follow until the run
    finishes. Every poll uses its own short session — a slow tail must not pin
    a connection. Reload-safe by construction: a fresh tail replays history."""
    from app.database import async_session

    yield _ev("assist_turn_started", {"run_id": run_id})
    sent = 0
    last_growth = asyncio.get_event_loop().time()
    while True:
        async with async_session() as db:
            row = (await db.execute(
                _sqltext("SELECT status, frames FROM assist_turn_runs WHERE id = :rid"),
                {"rid": run_id},
            )).mappings().first()
        if not row:
            yield _ev("error", {"detail": f"turn run not found: {run_id}"})
            return
        frames = row["frames"] or []
        if len(frames) > sent:
            last_growth = asyncio.get_event_loop().time()
        for f in frames[sent:]:
            yield _ev(f.get("e") or "error", f.get("d") or {})
        sent = len(frames)
        if row["status"] != "running":
            return
        # §17.875 — stall cap: never follow a wedged run forever.
        if (asyncio.get_event_loop().time() - last_growth) > _TAIL_STALL_SECONDS:
            yield _ev("error", {"detail": "This turn has gone quiet for over 6 minutes — it may still finish in the background (check the transcript later), but I'm releasing your screen. You can resend your message."})
            yield _ev(ASSIST_TURN_DONE, {"handled": "stalled_tail"})
            return
        await asyncio.sleep(0.6)


async def sweep_zombie_runs() -> int:
    """§17.875 — called at startup: any row still 'running' predates this boot
    (the drivers died with the old process) and can never finish. Mark it dead
    with an honest terminal frame so tails end and resume skips it."""
    from app.database import async_session

    dead_frames = _json.dumps([
        {"e": "error", "d": {"detail": "The engine restarted mid-turn — please resend your message."}},
        {"e": ASSIST_TURN_DONE, "d": {"handled": "died"}},
    ])
    async with async_session() as db:
        res = await db.execute(
            _sqltext("UPDATE assist_turn_runs SET status = 'error', finished_at = now(), "
                     "frames = frames || CAST(:f AS jsonb) WHERE status = 'running'"),
            {"f": dead_frames},
        )
        await db.commit()
    return res.rowcount or 0


# §17.875 — tail stall cap: the longest legitimate frame gap is a research/
# guide model call (~2-3 min). A tail that sees NO new frames for this long on
# a still-'running' row is following something wedged — end honestly rather
# than spin forever (the run may yet finish; its output lands in the
# transcript via the §17.873 captures).
_TAIL_STALL_SECONDS = 360


async def get_active_run(session_id: str) -> str | None:
    """The newest still-running turn for the session, for resume-on-load."""
    from app.database import async_session

    async with async_session() as db:
        return (await db.execute(
            _sqltext("SELECT id FROM assist_turn_runs WHERE session_id = :sid "
                     "AND status = 'running' ORDER BY created_at DESC LIMIT 1"),
            {"sid": session_id},
        )).scalar()


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
            # §17.874 — fixes are RESEARCH-BACKED, unconditionally. The live
            # incident: two consecutive fixes cycled GUESSED Servarr repo URLs
            # from training memory while the operator's paste showed the
            # keyring downloading as ASCII text (an error page) — the current
            # correct apt instructions are a fact only live research can
            # supply. The operator's standing requirement: unsure → research →
            # derive from up-to-date information. Costs ~a minute; the status
            # frame carries it.
            yield _ev(ASSIST_TURN_STATUS, {"text": "Diagnosing the error — researching current, up-to-date fixes for it (this can take a minute or two)…"})
            fix = await assist_agent.run_step_fix(
                session_id=session_id, node_key=nk,
                error=(d.get("error_text") or text_), history=history,
                research=True, db=db,
            )
            # §17.876 — honest, actionable fallback. "(no fix returned)" was a
            # dead end: it told the operator nothing and suggested nothing. The
            # empty case is now rare (think-off rescue), but when it happens the
            # operator should know it's a transient generation failure, not a
            # verdict on their problem.
            fix_text = (fix or {}).get("fix") or (
                "I couldn't produce a fix this time — the model returned no "
                "usable answer after several attempts. This is a generation "
                "hiccup, not a verdict on your problem. Press Send again to "
                "retry (research is re-run fresh), or paste just the last "
                "~50 lines of the error output to tighten the context."
            )
            yield _ev(ASSIST_ANSWER, {"kind": "fix", "text": fix_text})
            # §17.873 — the answer must outlive the run row: capture it as an
            # assistant turn (in-capture dedupe absorbs replays) so the
            # transcript — the UI's source of truth — carries it.
            try:
                await assist_agent.capture_assistant_reply(
                    session_id=session_id, node_key=nk, kind="fix",
                    content=fix_text, db=db,
                )
            except Exception:  # noqa: BLE001 — capture is best-effort
                logger.warning("turn_loop_fix_capture_failed sid=%s", session_id)
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
                try:  # §17.873 — durable transcript capture (dedupe-safe)
                    await assist_agent.capture_assistant_reply(
                        session_id=session_id, node_key=nk, kind="ask",
                        content=answer, db=db,
                    )
                except Exception:  # noqa: BLE001
                    logger.warning("turn_loop_ask_capture_failed sid=%s", session_id)
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

        # 5a. §17.869 (operator requirement) — UNSURE about a question means
        # RESEARCH, not a walkthrough rerun. When the decision layer couldn't
        # confidently route a question-shaped message, obtain the information
        # instead of guessing: the job-aware research path (§17.650) grounds
        # the answer in the project's own state + retrieval.
        import re as _re
        _questionish = _re.search(
            r"\?\s*$|^(can|could|how|what|why|where|which|who|should|is|are|do|does|will|would)\b",
            text_, _re.IGNORECASE)
        if _questionish:
            yield _ev(ASSIST_TURN_STATUS, {"text": "I'm not certain how to act on that — researching it against the project's current state…"})
            try:
                res = await assist_agent.run_step_research(
                    session_id=session_id, node_key=nk, question=text_,
                    history=history, db=db,
                )
                answer = (res or {}).get("answer") or ""
                if answer.strip():
                    yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": answer})
                    try:  # §17.873 — durable transcript capture (dedupe-safe)
                        await assist_agent.capture_assistant_reply(
                            session_id=session_id, node_key=nk, kind="ask",
                            content=answer, db=db,
                        )
                    except Exception:  # noqa: BLE001
                        logger.warning("turn_loop_research_capture_failed sid=%s", session_id)
                    handled["v"] = "research_fallback"
                    return
            except Exception as exc:  # noqa: BLE001
                logger.warning("turn_loop_research_fallback_failed sid=%s err=%r",
                               session_id, exc)

        # 5b. Fallback (low confidence / unhandled action): the progress
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
