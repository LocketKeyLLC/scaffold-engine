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

_TURN_TASKS: set = set()  # §17.888 — strong refs for detached drivers

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
    # §17.888 (audit #13) — hold a STRONG reference: a bare create_task can be
    # GC'd mid-turn (never reaching its finalizer → §17.875-style zombie run).
    task = asyncio.create_task(_drive_turn_run(
        run_id=run_id, session_id=session_id, message=message,
        command=command, node_key=node_key, history=history,
    ))
    _TURN_TASKS.add(task)
    task.add_done_callback(_TURN_TASKS.discard)
    return run_id


async def _append_frames(run_id: str, frames: list[_Event], db) -> None:
    payload = _json.dumps([{"e": n, "d": d} for n, d in frames])
    try:
        await db.execute(
            _sqltext("UPDATE assist_turn_runs SET frames = frames || CAST(:f AS jsonb) WHERE id = :rid"),
            {"rid": run_id, "f": payload},
        )
        await db.commit()
    except Exception:  # noqa: BLE001 — §17.888(#14) chokepoint: a fail-soft
        # somewhere upstream may have poisoned the shared transaction
        # (InFailedSQLTransactionError); one rollback + retry saves the frame —
        # and with it the generated answer — instead of erroring the whole run.
        await db.rollback()
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

        # 2a. §17.899 — "that wasn't actually done". Runs BEFORE the decision
        # layer and before orientation, because every downstream step reads the
        # completed-work digest: while a step is wrongly `done`, the decision
        # model, the guide, and the verifier are all reasoning from a false
        # premise. Deterministic + tightly bounded (see reopen_denied_step);
        # a no-op returns None and the turn continues normally.
        reopened = await assist_agent.reopen_denied_step(
            session_id=session_id, message=text_, db=db,
        )
        if reopened:
            yield _ev(ASSIST_TURN_ROUTED, {"action": "reopen", "override": "denial"})
            yield _ev(ASSIST_STEP_OUTCOME, {
                "node_key": reopened["node_key"], "status": "reopened",
            })
            yield _ev(ASSIST_TURN_STATUS, {"text": (
                f"↩︎ Got it — I'd marked **{reopened['node_key']}: "
                f"{reopened['title']}** done, and you're telling me it wasn't. "
                "Reopening it and picking that step back up."
            )})
            # node_key=None so the claim path resolves the (now reopened) step.
            async for e in _claim_and_guide(session_id, None, history, db,
                                            orient=False):
                yield e
            handled["v"] = "reopen"
            return

        # 2b. §17.903 — the operator is BLOCKED, not merely erroring. This runs
        # ahead of the decision layer because being unable to reach the step at
        # all is the dominant fact of the turn: the plan's premise is broken, so
        # any walkthrough for the current step is answering the wrong question.
        # Live failure: "i hit the reboot now and its still hung up" while the
        # pointer sat on "Install PalWorld server" — the next guide opened with
        # `sudo apt update` on a VM whose own Prerequisites said it must be
        # "fully installed and reachable", the exact thing just reported broken.
        if assist_policy.looks_like_blocked(text_):
            async for e in _blocked_flow(session_id, text_, node_key, history, db):
                yield e
            handled["v"] = "blocked"
            return

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
            # §17.903 — recording is not answering. A pivot framed as a QUESTION
            # was overridden ask→note, filed, and the turn ENDED — the operator's
            # direct "delete this VM and start over?" got no reply at all, and
            # they pressed Guide out of the silence, straight into a walkthrough
            # whose premise was already broken. The note still gets recorded (the
            # plan impact matters); it just no longer swallows the answer.
            q = (d.get("answer_query") or "").strip()
            if q:
                async for e in _answer(session_id, q, nk, history, db,
                                       status_text="Recorded that — now answering your question…"):
                    yield e
            handled["v"] = "note"
            return
        if confident and action == "submit":
            done = False
            blocked_reason = None
            async for e in _submit(session_id, d, text_, nk, history, db):
                if e[0] == ASSIST_STEP_OUTCOME:
                    if e[1].get("status") == "committed":
                        done = True
                    elif e[1].get("status") in ("step_incomplete", "verification_failed"):
                        blocked_reason = e[1].get("verify_reason") or "the step's goal isn't met yet"
                yield e
            if done:
                async for e in _claim_and_guide(session_id, None, history, db,
                                                orient=False):
                    yield e
            elif blocked_reason is not None:
                # §17.884 — a blocked submit must NEVER dead-end. Live incident:
                # the operator ran the discovery command the engine asked for,
                # pasted the ground truth back, the verifier (correctly) said
                # "step not complete" — and the turn ENDED, discarding the very
                # information the engine had requested. Continue into the fix
                # flow seeded with the evidence + the verifier's reason: the
                # pasted values are now provenance-legal grounding, so the next
                # command can use them directly.
                async for e in _fix_flow(
                    session_id, nk,
                    (f"{text_}\n\n[Progress noted, but the step is not complete "
                     f"yet — verifier: {blocked_reason}] Continue from the "
                     "output above: use the concrete values it contains."),
                    history, db,
                    status_text="Good progress — the step isn't finished yet, so I'm working out your next move from what you just pasted…",
                ):
                    yield e
            handled["v"] = "submit"
            return
        if confident and action == "skip":
            # §17.886(#2) — explicit skip was silently answered with a re-guide.
            from app.routers.assist import AssistSubmitInput, assist_submit
            try:
                yield _ev(ASSIST_TURN_STATUS, {"text": "⏩ Skipping this step (recorded — you can revisit it later)…"})
                await assist_submit(
                    session_id,
                    AssistSubmitInput(node_key=nk, output=text_, action="skip",
                                      history=history),
                    db=db,
                )
                yield _ev(ASSIST_STEP_OUTCOME, {"node_key": nk, "status": "skipped"})
                async for e in _claim_and_guide(session_id, None, history, db, orient=False):
                    yield e
            except Exception as exc:  # noqa: BLE001
                yield _ev(ASSIST_TURN_STATUS, {"text": f"Couldn't skip that step ({exc}). It stays open."})
            handled["v"] = "skip"
            return
        if confident and action in ("advance", "finalize"):
            # §17.886(#2) — run the tracker reconcile, then honor EVERY result
            # action (the old code matched only 'advanced', so 'finalized' and
            # 'added_step' re-guided the stale node).
            async for e in _track_then_continue(session_id, text_, nk, history, db,
                                                finalize=(action == "finalize")):
                yield e
            handled["v"] = action
            return
        if confident and action == "pause":
            from app.routers.assist import assist_pause
            try:
                await assist_pause(session_id, db=db)
                yield _ev(ASSIST_TURN_STATUS, {"text": "⏸ Session paused — say \"resume\" whenever you're ready and we'll pick up exactly here."})
            except Exception as exc:  # noqa: BLE001
                yield _ev(ASSIST_TURN_STATUS, {"text": f"Couldn't pause ({exc})."})
            handled["v"] = "pause"
            return
        if confident and action == "add_step":
            from app.routers.assist import AssistAddStepInput, assist_add_step
            try:
                yield _ev(ASSIST_TURN_STATUS, {"text": "➕ Adding that as its own step…"})
                res = await assist_add_step(
                    session_id, AssistAddStepInput(request=text_, before_node_key=nk), db=db,
                )
                new_nk = (res or {}).get("node_key")
                async for e in _claim_and_guide(session_id, new_nk, history, db, orient=False):
                    yield e
            except Exception as exc:  # noqa: BLE001
                yield _ev(ASSIST_TURN_STATUS, {"text": f"Couldn't add the step ({exc}) — tell me again with a bit more detail."})
            handled["v"] = "add_step"
            return
        if confident and action == "handoff":
            yield _ev(ASSIST_TURN_STATUS, {"text": "🤝 To hand this step to the engine, press the step's Handoff button in the panel — chat-initiated handoff isn't wired yet, and I'd rather tell you that than pretend."})
            handled["v"] = "handoff"
            return
        if confident and action == "explain_plan":
            from app.routers.assist import assist_get_checklist
            try:
                cl = await assist_get_checklist(session_id, db=db)
                items = (cl or {}).get("steps") or (cl or {}).get("checklist") or []
                lines = [f"- {'✅' if (i.get('status') in ('committed','skipped')) else '👉' if i.get('node_key')==nk else '·'} {i.get('node_key')}: {(i.get('title') or '')[:70]}" for i in items[:30]]
                yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": "**The plan so far:**\n" + "\n".join(lines)})
            except Exception as exc:  # noqa: BLE001
                yield _ev(ASSIST_TURN_STATUS, {"text": f"Couldn't render the plan ({exc})."})
            handled["v"] = "explain_plan"
            return
        if confident and action in ("set_env", "set_verbosity"):
            import re as _re2
            from app.routers.assist import AssistEnvInput, assist_set_env
            try:
                subs = dict(_re2.findall(r"([A-Za-z_]\w*)=(\S+)", text_))
                verb = ("terse" if "terse" in text_.lower() else
                        "detailed" if "detail" in text_.lower() else
                        "normal" if action == "set_verbosity" else None)
                await assist_set_env(
                    session_id,
                    AssistEnvInput(substitutions=subs or None, verbosity=verb),
                    db=db,
                )
                yield _ev(ASSIST_TURN_STATUS, {"text": "Noted — environment updated."})
            except Exception as exc:  # noqa: BLE001
                yield _ev(ASSIST_TURN_STATUS, {"text": f"Couldn't update the environment ({exc})."})
            handled["v"] = "set_env"
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
            async for e in _fix_flow(
                session_id, nk, text_, history, db,  # §17.886(#4) — full paste, not the ≤2000-char echo
                status_text="Diagnosing the error — researching current, up-to-date fixes for it (this can take a minute or two)…",
            ):
                yield e
            if impact == "surface":
                async for e in _surface(session_id, d, text_, nk, db):
                    yield e
            handled["v"] = "fix"
            return
        if confident and action in ("ask", "question"):
            yield _ev(ASSIST_TURN_STATUS, {"text": "Researching your question against the project's current state — this can take a minute or two…"})
            res = await assist_agent.run_step_research(
                session_id=session_id, node_key=nk,
                question=text_, history=history, db=db,  # §17.886(#4)
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
        async for e in _track_then_continue(session_id, text_, nk, history, db):
            yield e
        handled["v"] = "fallback"


async def _note(session_id: str, d: dict, text_: str, nk, db) -> AsyncIterator[_Event]:
    yield _ev(ASSIST_TURN_STATUS, {"text": "Recording that and checking whether it changes the plan…"})
    from app.routers.assist import AssistNoteInput, assist_note
    kind = d.get("note_kind") or ("decision" if (d.get("plan_impact") == "reshape") else "note")
    res = await assist_note(
        session_id,
        AssistNoteInput(text=text_, kind=kind, node_key=nk),  # §17.886(#4) — reset-intent regexes need the original words
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



async def _track_then_continue(session_id: str, text_: str, nk, history, db,
                               *, finalize: bool = False) -> AsyncIterator[_Event]:
    """§17.886(#2) — tracker reconcile honoring EVERY result action."""
    from app.modules import assist_agent
    from app.routers.assist import AssistInterpretInput, assist_track
    tr = {}
    try:
        yield _ev(ASSIST_TURN_STATUS, {"text": "Checking step progress…"})
        tr = await assist_track(
            session_id, AssistInterpretInput(message=text_, node_key=nk, history=history),
            db=db,
        ) or {}
    except Exception as exc:  # noqa: BLE001 — §17.885 lesson: log LOUD
        logger.error("turn_loop_track_failed sid=%s err=%r", session_id, exc)
    act = tr.get("action")
    if act in ("advanced", "finalized"):
        yield _ev(ASSIST_STEP_OUTCOME, {
            "node_key": tr.get("retired_prior_step") or nk, "status": "committed",
        })
    if act == "finalized":
        yield _ev(ASSIST_TURN_STATUS, {"text": "🎉 That was the last step — the project is complete! Compiling the summary is available via the session's Done view."})
        return
    if act == "added_step":
        async for e in _claim_and_guide(session_id, tr.get("node_key"), history, db,
                                        orient=False):
            yield e
        return
    async for e in _claim_and_guide(
        session_id, None if act in ("advanced",) else nk, history, db, orient=False,
    ):
        yield e


async def _answer(session_id: str, question: str, nk, history, db,
                  *, status_text: str) -> AsyncIterator[_Event]:
    """§17.903 — the shared "answer the operator's question" tail.

    Extracted because more than one branch now needs it: a recorded note that
    also asked something, and the blocked flow. Every path that reaches an
    operator turn must leave them with an answer — a branch that records and
    returns is the shape that produced the silent dead end."""
    from app.modules import assist_agent
    yield _ev(ASSIST_TURN_STATUS, {"text": status_text})
    try:
        res = await assist_agent.run_step_research(
            session_id=session_id, node_key=nk, question=question,
            history=history, db=db,
        )
        answer = (res or {}).get("answer") or ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("turn_loop_answer_failed sid=%s err=%r", session_id, exc)
        answer = ""
    if not answer.strip():
        # §17.876 posture — an honest, actionable fallback beats silence, which
        # is the whole point of this function existing.
        answer = ("I couldn't put together a grounded answer for that just now. "
                  "Tell me what you're seeing on screen right now and I'll pick "
                  "it up from there.")
    yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": answer})
    try:  # §17.873 — answers must outlive the run row
        await assist_agent.capture_assistant_reply(
            session_id=session_id, node_key=nk, kind="ask", content=answer, db=db,
        )
    except Exception:  # noqa: BLE001 — capture is best-effort
        logger.warning("turn_loop_answer_capture_failed sid=%s", session_id)


async def _blocked_flow(session_id: str, text_: str, node_key, history, db
                        ) -> AsyncIterator[_Event]:
    """§17.903 — the operator can't reach the current step; work the BLOCKER.

    Three things happen, in this order, and the order is the point:
      1. Say plainly that the step's premise is broken. The operator had been
         handed guidance that assumed the opposite; naming the contradiction is
         what turns "the engine isn't listening" back into a conversation.
      2. Diagnose the blocker itself (research-backed fix flow), seeded with the
         blocker text rather than the step's task — the step is not the problem.
      3. Record it as a plan-affecting note so §17.677 can SURFACE a plan change
         for approval. Operator-confirmed: work the blocker in place, never
         mutate the plan unasked (the §17.891 lesson).
    """
    from app.modules import assist_agent
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    nk = node_key or (sess or {}).get("current_node_key")
    title = ""
    if nk:
        try:
            from sqlalchemy import text as _text
            title = (await db.execute(_text(
                "SELECT n.title FROM dag_nodes n JOIN assist_sessions s "
                "ON s.job_id = n.job_id WHERE s.id = :sid AND n.node_key = :nk"),
                {"sid": session_id, "nk": nk})).scalar() or ""
        except Exception:  # noqa: BLE001 — the callout degrades, never fails
            title = ""

    step_label = f"**{nk}: {title}**" if title else (f"**{nk}**" if nk else "this step")
    yield _ev(ASSIST_ANSWER, {"kind": "track", "text": (
        f"⚠️ You're blocked, so {step_label} can't move yet — its starting point "
        f"isn't true right now. I'm working the blocker itself, not the step.")})

    async for e in _fix_flow(
        session_id, nk, text_, history, db,
        status_text="Diagnosing what's actually blocking you — researching current fixes for it (this can take a minute or two)…",
    ):
        yield e

    # The plan may well need to change (a rebuild, an inserted step). Surface it
    # for approval rather than applying it — see the docstring.
    try:
        from app.routers.assist import AssistNoteInput, assist_note
        res = await assist_note(
            session_id,
            # "constraint" is the closest kind the AssistNoteInput Literal
            # allows — a blocker genuinely constrains what the plan can do next,
            # and it rides the same §17.677 impact pass. ("blocker" is not in
            # the enum; passing it would 422 the whole turn.)
            AssistNoteInput(text=text_, kind="constraint", node_key=nk),
            db=db,
        )
        if res.get("replan_proposal"):
            yield _ev(ASSIST_REPLAN_PROPOSAL, {"proposal": res["replan_proposal"]})
    except Exception as exc:  # noqa: BLE001 — surfacing is an enhancement
        logger.warning("turn_loop_blocked_note_failed sid=%s err=%r", session_id, exc)


async def _fix_flow(session_id: str, nk, error_text: str, history, db,
                    *, status_text: str) -> AsyncIterator[_Event]:
    """§17.874/884 — the research-backed fix sequence, shared by the fix
    dispatch branch and the incomplete-submit continuation."""
    from app.modules import assist_agent
    yield _ev(ASSIST_TURN_STATUS, {"text": status_text})
    fix = await assist_agent.run_step_fix(
        session_id=session_id, node_key=nk, error=error_text,
        history=history, research=True, db=db,
    )
    # §17.876 — honest, actionable fallback (never a silent dead end).
    fix_text = (fix or {}).get("fix") or (
        "I couldn't produce a fix this time — the model returned no "
        "usable answer after several attempts. This is a generation "
        "hiccup, not a verdict on your problem. Press Send again to "
        "retry (research is re-run fresh), or paste just the last "
        "~50 lines of the error output to tighten the context."
    )
    yield _ev(ASSIST_ANSWER, {"kind": "fix", "text": fix_text})
    try:  # §17.873 — answers must outlive the run row
        await assist_agent.capture_assistant_reply(
            session_id=session_id, node_key=nk, kind="fix",
            content=fix_text, db=db,
        )
    except Exception:  # noqa: BLE001 — capture is best-effort
        logger.warning("turn_loop_fix_capture_failed sid=%s", session_id)


def _is_must_claim_first(exc) -> bool:
    """§17.878 — recognize the recoverable submit refusal (step never claimed)."""
    detail = getattr(exc, "detail", None)
    return "must_claim_first" in (
        str(detail) if detail is not None else str(exc)
    )


async def _submit(session_id: str, d: dict, text_: str, nk, history, db) -> AsyncIterator[_Event]:
    yield _ev(ASSIST_TURN_STATUS, {"text": "Recording the result and verifying the step…"})
    from app.routers.assist import AssistSubmitInput, assist_submit, assist_next

    async def _try_submit():
        return await assist_submit(
            session_id,
            AssistSubmitInput(node_key=nk, output=text_,  # §17.886(#4) — verbatim, never the decide paraphrase
                              action="submit", history=history),
            db=db,
        )

    try:
        try:
            res = await _try_submit()
        except Exception as exc:  # noqa: BLE001
            # §17.878 — SELF-HEAL the unclaimed-step trap. Live incident: the
            # tracker committed T13 and moved the session pointer to T14 without
            # a formal claim (presented_at NULL); guide/fix flowed all day off
            # the pointer, then the operator's SUCCESSFUL install evidence hit
            # the one endpoint that enforces the claim and was refused (409
            # must_claim_first) — the step never committed and the walkthrough
            # replayed ("it backlogged again"). must_claim_first is trivially
            # recoverable: claim (assist_next presents the earliest claimable
            # pending step — the pointer step) and retry ONCE. Any other
            # refusal keeps the §17.863 explain-and-continue behavior.
            if not _is_must_claim_first(exc):
                raise
            yield _ev(ASSIST_TURN_STATUS, {
                "text": "The step wasn't formally claimed (a bookkeeping hiccup, not your result) — claiming it now and recording your result…",
            })
            await assist_next(session_id, db=db)
            res = await _try_submit()
        yield _ev(ASSIST_STEP_OUTCOME, {
            "node_key": nk, "status": (res or {}).get("status") or "recorded",
            # §17.884 — the verifier's reason rides the outcome frame so the
            # dispatch can CONTINUE a blocked submit instead of dead-ending.
            "verify_reason": (((res or {}).get("success_verdict") or {}).get("reason") or ""),
        })
        # §17.889(#3) — a deliberating decision step computed a needs-input
        # question and THREW IT AWAY (rendered as a bare toast). Surface +
        # capture it like any other engine answer.
        dm = (res or {}).get("decision_message") or ""
        if dm.strip() and (res or {}).get("status") == "deliberating":
            yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": dm})
            try:
                from app.modules import assist_agent as _aa
                await _aa.capture_assistant_reply(
                    session_id=session_id, node_key=nk, kind="ask", content=dm, db=db)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001 — a refused submit must not kill the turn
        # §17.889(#11) — durable answer (status lines vanish at turn end) and an
        # actual continuation instead of a dangling "Continuing…".
        msg = (f"I recorded what you pasted, but the step wouldn't accept it as a "
               f"submission ({exc}). Here's where things stand instead:")
        yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": msg})
        try:
            async for e in _claim_and_guide(session_id, nk, history, db, orient=True):
                yield e
        except Exception:  # noqa: BLE001 — orientation is best-effort here
            logger.warning("turn_loop_refused_submit_orient_failed sid=%s", session_id)


async def _claim_and_guide(
    session_id: str, node_key, history, db, *, orient: bool,
) -> AsyncIterator[_Event]:
    """Claim (when needed) → premise-verify → stream the walkthrough. The one
    sequence whose client-side composition caused every 'stuck' report."""
    from app.modules import assist_agent
    from app.routers.assist import assist_next

    sess = await assist_agent.get_session(session_id=session_id, db=db)
    nk = node_key or (sess or {}).get("current_node_key")
    if nk:
        # §17.878/880 — pointer sanity at the guide chokepoint. Two live-hit
        # stale-pointer shapes, both from paths that move state without full
        # bookkeeping:
        #   'pending'  — pointer moved without a formal claim → later submits
        #                409 must_claim_first (§17.878: T14 ran a day unclaimed).
        #                Repair: claim it.
        #   terminal   — step retired (tracker advance / commit race) but the
        #                pointer stayed → every Guide/Done press re-walks the
        #                FINISHED step ("this node is done", §17.880 live
        #                incident). Repair: announce + heal forward into the
        #                normal claim path below (which claims the next step
        #                and re-points the session).
        # generate_step_guidance_stream's §17.639 guard can't cover this: we
        # pass it an explicit node_key, which it honors by contract.
        # Fail-soft: repair must never block the walkthrough.
        try:
            from sqlalchemy import text as _text
            st = (await db.execute(_text(
                "SELECT status FROM assist_steps"
                " WHERE session_id = :sid AND node_key = :nk"),
                {"sid": session_id, "nk": nk})).scalar()
            if st == "pending":
                logger.info("turn_loop_claim_repair sid=%s nk=%s", session_id, nk)
                await assist_next(session_id, db=db)
            elif st in assist_agent._TERMINAL_STEP_STATUSES:
                logger.info("turn_loop_terminal_pointer_heal sid=%s nk=%s st=%s",
                            session_id, nk, st)
                yield _ev(ASSIST_TURN_STATUS, {
                    "text": f"✅ Step {nk} is already done — moving on to the next step…",
                })
                nk = None
        except Exception as exc:  # noqa: BLE001
            logger.warning("turn_loop_claim_repair_failed sid=%s err=%r", session_id, exc)
    if not nk:
        yield _ev(ASSIST_TURN_STATUS, {"text": "Finding the next step and verifying it against what we know…"})
        nxt = await assist_next(session_id, db=db)
        nk = (nxt or {}).get("node_key")
        pc = (nxt or {}).get("premise_check") or {}
        if pc.get("stale"):
            yield _ev(ASSIST_TURN_STATUS, {"text": f"⚠ Before we walk into step {nk}: {pc.get('reason') or 'its premise may be out of date.'}"})
        if not nk:
            # §17.889(#9) — say WHICH terminal state, not a shrug.
            st = (nxt or {}).get("status") or ""
            if st == "completed":
                yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": "🎉 **Every step in this plan is done — the project is complete.** Open the session's Done view for the compiled summary of what you built."})
            elif st == "paused":
                yield _ev(ASSIST_TURN_STATUS, {"text": "⏸ This session is paused — say \"resume\" to pick up where you left off."})
            else:
                try:
                    from app.modules.assist_notes import get_pending_replan
                    pend = await get_pending_replan(session_id=session_id, db=db)
                except Exception:  # noqa: BLE001
                    pend = None
                if pend:
                    yield _ev(ASSIST_TURN_STATUS, {"text": "A plan-change proposal is waiting for your decision (see the card above) — answer it and we'll continue."})
                else:
                    yield _ev(ASSIST_TURN_STATUS, {"text": "No claimable step right now — a step may be mid-verification; try again in a moment or press Done if you believe the plan is finished."})
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
            if ev.get("status") not in ("ready", "presented", None):
                # §17.889(#12) — a failed generation rendered NOTHING in the
                # SPA ("pressed Guide, saw nothing"). Honest fallback frame.
                yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": "I couldn't generate the walkthrough just now (the model returned nothing usable). Press Guide again to retry — nothing is stuck."})
            yield _ev(ASSIST_GUIDE_DONE, {
                "status": ev.get("status"), "node_key": nk,
                "guidance_meta": ev.get("guidance_meta") or {},
                "cached": ev.get("cached", False),
            })
