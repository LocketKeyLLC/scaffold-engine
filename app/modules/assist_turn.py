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

from app.config import settings
from typing import AsyncIterator

from app.modules import assist_policy
from app.sse_events import (
    ASSIST_ANSWER,
    ASSIST_TURN_PULSE,
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
from app.utils.cost_tracking import current_job_id


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


async def _append_frames(run_id: str, frames: list[_Event], db,
                         stamps: list[int] | None = None) -> None:
    # §17.1109 — ``t`` = ms since the turn started, stamped at yield time (the
    # driver passes ``stamps``); frames read back without it are pre-§17.1109.
    if stamps is not None and len(stamps) == len(frames):
        payload = _json.dumps([{"e": n, "d": d, "t": t}
                               for (n, d), t in zip(frames, stamps, strict=True)])
    else:
        payload = _json.dumps([{"e": n, "d": d} for n, d in frames])
    try:
        await db.execute(
            _sqltext("UPDATE assist_turn_runs SET frames = frames || CAST(:f AS jsonb) WHERE id = :rid"),
            {"rid": run_id, "f": payload},
        )
        await db.commit()
    except Exception:
        # somewhere upstream may have poisoned the shared transaction
        # (InFailedSQLTransactionError); one rollback + retry saves the frame —
        # and with it the generated answer — instead of erroring the whole run.
        await db.rollback()
        await db.execute(
            _sqltext("UPDATE assist_turn_runs SET frames = frames || CAST(:f AS jsonb) WHERE id = :rid"),
            {"rid": run_id, "f": payload},
        )
        await db.commit()


async def _append_note_detached(run_id: str, text_value: str) -> None:
    """§17.1082 — a progress note from deep code (model retry) lands on the run
    row through its OWN short session, with a lock timeout: the driver's
    session never holds this row between its committed appends, but a bounded
    wait is the guard against ever waiting on it (§17.1052's lesson)."""
    from app.database import async_session
    payload = _json.dumps([{"e": ASSIST_TURN_STATUS, "d": {"text": text_value}}])
    try:
        async with async_session() as db:
            await db.execute(_sqltext("SET LOCAL lock_timeout = '2s'"))
            await db.execute(
                _sqltext("UPDATE assist_turn_runs SET frames = frames || CAST(:f AS jsonb) WHERE id = :rid"),
                {"rid": run_id, "f": payload},
            )
            await db.commit()
    except Exception as exc:
        logger.warning("turn_note_append_failed run_id=%s err=%r", run_id, exc)


async def _drive_turn_run(
    *, run_id: str, session_id: str, message: str | None, command: str,
    node_key: str | None, history: list[dict],
) -> None:
    """Run the loop to completion on its OWN session (§17.621 pattern),
    appending frames as they happen. Guide deltas are coalesced (~0.7s) so a
    long walkthrough doesn't hammer the row with per-token commits."""
    from app.database import async_session
    from app.utils.progress import reset_turn_note_sink, set_turn_note_sink
    from app.utils import turn_timing

    status = "done"
    # §17.1109 (ledger L-1) — every operator-facing status frame opens a
    # timed stage; model calls made inside it accrue to it (cost_tracking
    # reads the ContextVar). The record lands on the run row at finalize.
    timer = turn_timing.TurnTimer()
    _timer_token = turn_timing.current_turn_timer.set(timer)
    # §17.1140 (ledger O-1) — attribute every model call in this turn to the
    # session's JOB: llm_call_logs / llm_traces / cost rollups were job-less
    # for assist turns (only the executor set the ContextVar), so a captured
    # assist prompt could not be found from the job's Traces tab.
    _job_token = None
    try:
        async with async_session() as _jdb:
            _jid = (await _jdb.execute(
                _sqltext("SELECT job_id FROM assist_sessions WHERE id = :sid"), {"sid": session_id},
            )).scalar()
        if _jid:
            _job_token = current_job_id.set(str(_jid))
    except Exception as exc:
        logger.debug("assist_turn_job_attribution_failed sid=%s err=%r", session_id, exc)
    # §17.1082 — deep code (the model router's retry loop) can say one line to
    # the operator's status line while this driver is blocked inside the loop.
    _note_tasks: set[asyncio.Task] = set()

    def _sink(text_value: str) -> None:
        t = asyncio.create_task(_append_note_detached(run_id, text_value))
        _note_tasks.add(t)
        t.add_done_callback(_note_tasks.discard)
    _sink_token = set_turn_note_sink(_sink)
    try:
        async with async_session() as db:
            buf: list[_Event] = []
            stamps: list[int] = []
            last_flush = asyncio.get_event_loop().time()
            async for ev in run_turn(
                session_id=session_id, message=message, command=command,
                node_key=node_key, history=history, db=db,
            ):
                if ev[0] == ASSIST_TURN_STATUS:
                    timer.mark((ev[1] or {}).get("text"))
                elif ev[0] == ASSIST_TURN_ROUTED:
                    timer.annotate("action", (ev[1] or {}).get("action"))
                buf.append(ev)
                stamps.append(timer.elapsed_ms())
                now = asyncio.get_event_loop().time()
                if ev[0] != ASSIST_GUIDE_DELTA or (now - last_flush) >= 0.7:
                    await _append_frames(run_id, buf, db, stamps=stamps)
                    buf, stamps, last_flush = [], [], now
            if buf:
                await _append_frames(run_id, buf, db, stamps=stamps)
    except Exception as exc:
        status = "error"
        logger.exception("turn_run_failed run_id=%s", run_id)
        try:
            async with async_session() as db:
                await _append_frames(run_id, [("error", {"detail": str(exc)})], db)
        except Exception:
            pass
    finally:
        reset_turn_note_sink(_sink_token)
        if _note_tasks:
            await asyncio.gather(*_note_tasks, return_exceptions=True)
        turn_timing.current_turn_timer.reset(_timer_token)
        if _job_token is not None:
            current_job_id.reset(_job_token)  # §17.1140
        timings = timer.finish()
        logger.info("assist_turn_timing: run_id=%s session_id=%s status=%s %s",
                    run_id, session_id, status, timer.summary_line(timings))
        try:
            async with async_session() as db:
                await db.execute(
                    _sqltext("UPDATE assist_turn_runs SET status = :st, finished_at = now(), "
                             "timings = CAST(:tm AS jsonb) WHERE id = :rid"),
                    {"rid": run_id, "st": status, "tm": _json.dumps(timings)},
                )
                await db.commit()
        except Exception:
            logger.exception("turn_run_finalize_failed run_id=%s", run_id)


async def tail_turn_run(run_id: str) -> AsyncIterator[_Event]:
    """Yield the run's frames from the beginning, then follow until the run
    finishes. Every poll uses its own short session — a slow tail must not pin
    a connection. Reload-safe by construction: a fresh tail replays history."""
    from app.database import async_session

    yield _ev("assist_turn_started", {"run_id": run_id})
    sent = 0
    started = last_growth = asyncio.get_event_loop().time()
    last_pulse = started
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
        # §17.1082 — liveness pulse: the row is still 'running' and nothing
        # new has landed for a while. Not persisted; a quiet 105 s research
        # pass looked like a dead page (live, 2026-09-15).
        _now = asyncio.get_event_loop().time()
        if (_now - last_growth) >= _PULSE_AFTER_S and (_now - last_pulse) >= _PULSE_EVERY_S:
            last_pulse = _now
            yield _ev(ASSIST_TURN_PULSE, {"running_s": int(_now - started), "quiet_s": int(_now - last_growth)})
        # §17.875 — stall cap: never follow a wedged run forever.
        if (asyncio.get_event_loop().time() - last_growth) > _TAIL_STALL_SECONDS:
            yield _ev("error", {"detail": "This turn has gone quiet for over 6 minutes — it may still finish in the background (check the transcript later), but I'm releasing your screen. You can resend your message."})
            yield _ev(ASSIST_TURN_DONE, {"handled": "stalled_tail"})
            return
        await asyncio.sleep(0.6)


async def sweep_zombie_runs(*, older_than_minutes: int | None = None) -> int:
    """§17.875 — called at startup: any row still 'running' predates this boot
    (the drivers died with the old process) and can never finish. Mark it dead
    with an honest terminal frame so tails end and resume skips it.

    §17.1078 — with ``older_than_minutes`` (the queue's periodic sweep) only
    runs older than that are closed, with wording that says what happened:
    the turn stalled, the engine did not restart."""
    from app.database import async_session

    detail = ("The engine restarted mid-turn — please resend your message." if older_than_minutes is None
              else f"This turn stalled for more than {older_than_minutes} minutes and was closed — please resend your message.")
    dead_frames = _json.dumps([
        {"e": "error", "d": {"detail": detail}},
        {"e": ASSIST_TURN_DONE, "d": {"handled": "died"}},
    ])
    where = "WHERE status = 'running'"
    params: dict = {"f": dead_frames}
    if older_than_minutes is not None:
        where += " AND created_at < now() - make_interval(mins => :mins)"
        params["mins"] = int(older_than_minutes)
    async with async_session() as db:
        res = await db.execute(
            _sqltext("UPDATE assist_turn_runs SET status = 'error', finished_at = now(), "
                     "frames = frames || CAST(:f AS jsonb) " + where),
            params,
        )
        await db.commit()
    return res.rowcount or 0


# §17.875 — tail stall cap: the longest legitimate frame gap is a research/
# guide model call (~2-3 min). A tail that sees NO new frames for this long on
# a still-'running' row is following something wedged — end honestly rather
# than spin forever (the run may yet finish; its output lands in the
# transcript via the §17.873 captures).
_TAIL_STALL_SECONDS = 360
# §17.1082 — pulse cadence: after 10 s of silence, one pulse every 10 s.
_PULSE_AFTER_S = 10
_PULSE_EVERY_S = 10


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

    if command == "verify_state":  # §17.1050 — the 🩺 button
        # §17.1104 — reconcile the PENDING plan against confirmed facts +
        # system map FIRST (complete already-done steps, propose dropping
        # obsolete/duplicate ones), so the state check runs on a clean plan.
        async for e in _reconcile_plan(session_id, node_key, db):
            yield e
        async for e in _start_state_check(session_id, node_key, db):
            yield e
        handled["v"] = "verify_state"
        return
    if command == "guide":
        # §17.950 — "Guide me" used to go STRAIGHT to claim-and-guide, with
        # no notion of whether the step was already finished. So an operator
        # who completed a step and pressed Guide me expecting to move on got
        # the same walkthrough back, forever.
        #
        # It now reuses the message path's gates EXACTLY — no weaker: the
        # operator's OWN most recent words on this step must carry a §17.891
        # advancement signal, AND the tracker must independently judge the
        # step done above the confidence threshold. A Guide press is not
        # itself evidence of anything, so a step with no such message behind
        # it re-guides exactly as before.
        _adv_msg = await _recent_advance_message(session_id, node_key, db)
        if _adv_msg:
            async for e in _track_then_continue(
                    session_id, _adv_msg, node_key, history, db):
                yield e
            handled["v"] = "guide_advanced"
            return
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
    except Exception as exc:
        logger.warning("turn_loop_capture_failed sid=%s err=%r", session_id, exc)

    # 1a′. §17.1050 — a pending state check: the operator pasted the probe
    # script's output (the `== id ==` markers attribute it), or asked for
    # one by phrase. Anything else clears the pending check and continues.
    try:
        from app.modules import assist_state_check as _sc
        if _sc.STATE_CHECK_PHRASE_RE.search(text_) and settings.assist_state_check_enabled:
            async for e in _start_state_check(session_id, node_key, db):
                yield e
            handled["v"] = "verify_state"
            return
        _pending_sc = await _sc.get_pending_state_check(db=db, session_id=session_id)
        if _pending_sc and _sc.looks_like_probe_output(text_):
            async for e in _resolve_state_check(session_id, node_key, text_, history, db):
                yield e
            handled["v"] = "state_check_resolved"
            return
        if _pending_sc and _sc.STATE_CHECK_SKIP_RE.search(text_):
            # §17.1138 — "skip the rest": finish with what was answered so far
            async for e in _resolve_state_check(session_id, node_key, "", history, db, finish=True):
                yield e
            handled["v"] = "state_check_finished"
            return
        if _pending_sc:
            await _sc.clear_pending_state_check(db=db, session_id=session_id)
    except Exception as exc:
        logger.warning("state_check_route_failed sid=%s err=%r", session_id, exc)

    # 1b. §17.951 — resolve a pending completion confirmation.
    #
    # Runs BEFORE the decision layer on purpose: a bare "yes" or "confirm"
    # carries no intent the classifier could route sensibly, and the ONLY
    # thing that makes reading it as a completion is that the engine just
    # asked. Scoping it to a staged offer is what makes a loose affirmative
    # safe — outside that window "yes" is just a word.
    try:
        from app.modules import assist_notes

        _offer = await assist_notes.get_pending_completion_confirm(
            session_id=session_id, db=db)
    except Exception as exc:
        logger.warning("completion_confirm_probe_failed sid=%s err=%r",
                       session_id, exc)
        _offer = None
    if _offer:
        _onk = _offer.get("node_key")
        # §17.970 — accept the claim wherever the operator put it. The
        # anchored `looks_like_confirmation` misses "based on the previous
        # commands i believe it is done, as well as the following: <paste>",
        # which is how they actually answered — three times, each of which
        # SUPERSEDED the offer instead of resolving it. Scoped to the staged
        # window, so §17.890's narrow bare claim still governs elsewhere.
        if (assist_policy.looks_like_confirmation(text_)
                or assist_policy.claims_completion_in_prose(text_)):
            yield _ev(ASSIST_TURN_STATUS, {
                "text": "Marking this step complete on your word…"})
            await _clear_completion_confirm(session_id, db)
            from app.routers.assist import AssistSubmitInput, assist_submit
            try:
                # The operator's affirmation IS the evidence (§17.890): a
                # bare claim is exempt from the verify hard-block, so this
                # commits rather than looping back through the same veto
                # that produced the offer.
                res = await assist_submit(
                    session_id,
                    AssistSubmitInput(
                        node_key=_onk,
                        output=(f"Operator confirmed this step is complete: "
                                f"{text_.strip()[:200]}"),
                        # §17.971 — say so as DATA. The text above embeds
                        # their message (paste and all), so re-deriving the
                        # §17.890 exemption from it fails exactly when the
                        # operator answers with evidence attached.
                        operator_affirmed=True,
                        action="submit", history=history),
                    db=db,
                ) or {}
                if res.get("committed"):
                    yield _ev(ASSIST_STEP_OUTCOME,
                              {"node_key": _onk, "status": "committed"})
                    logger.info(
                        "assist_completion_confirmed session_id=%s node_key=%s",
                        session_id, _onk)
                    async for e in _reconciliation_note(session_id, _onk, res, db):
                        yield e
                    async for e in _claim_and_guide(session_id, None, history,
                                                    db, orient=False):
                        yield e
                    handled["v"] = "completion_confirmed"
                    return
                logger.warning(
                    "completion_confirm_not_committed sid=%s nk=%s res=%r",
                    session_id, _onk, str(res)[:200])
            except Exception as exc:
                logger.error("completion_confirm_commit_failed sid=%s err=%r",
                             session_id, exc)
                yield _ev(ASSIST_TURN_STATUS, {
                    "text": f"Couldn't close the step out ({exc}) — it stays open."})
        elif assist_policy.looks_like_decline(text_):
            # Not done after all: drop the offer and carry on normally, so
            # the "no" is answered as a message rather than re-asked.
            await _clear_completion_confirm(session_id, db)
            logger.info("assist_completion_confirm_declined session_id=%s nk=%s",
                        session_id, _onk)
        else:
            # Anything else supersedes the offer — the operator has moved on
            # to something new and a stale "confirm?" must not linger.
            await _clear_completion_confirm(session_id, db)

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

    # 2a. §17.1144 — a question about the ENGINE's own capabilities is answered
    # from the recipe registry, never from the web. Live: "please assist me in
    # setting up the local runner on the proxmox server" was researched as
    # "SAX1V1K ES2251 node Proxmox local runner setup…" and answered as a plan
    # to build a CI-runner VM. Runs ahead of the decision layer because the
    # classifier has no action for "about the engine"; and its yes/no is a
    # STAGED offer (scoped, like the completion offer), so a bare "yes" here
    # opens the walkthrough instead of being routed as a claim.
    from app.modules import engine_setup as _es
    _setup_offer = await _es.get_pending_setup_offer(session_id=session_id, db=db)
    if _setup_offer:
        if assist_policy.looks_like_confirmation(text_):
            _rid = _setup_offer["recipe_id"]
            await _es.clear_pending_setup_offer(session_id=session_id, db=db)
            yield _ev(ASSIST_TURN_STATUS, {"text": "Opening the walkthrough as its own job…"})
            try:
                _started = await _es.start_recipe_for_session(db, session_id, _rid)
                _jid = str(_started.get("job_id") or "")
                _reply = (f"## Walkthrough opened\n\n**{_es.BY_ID[_rid].title}** is now its own job "
                          f"(`{_jid[:8]}…`) on the dashboard — open it there, approve its plan, and it walks you "
                          f"through the setup step by step. This session stays where it is — your current step here is unchanged.")
            except Exception as exc:
                logger.warning("setup_offer_start_failed sid=%s recipe=%s err=%r", session_id, _rid, exc)
                _reply = (f"I could not open the walkthrough ({str(exc)[:160]}). You can start it from "
                          f"**Capabilities → {_es.BY_ID[_rid].title}** in the console.")
            yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": _reply})
            try:
                await assist_agent.capture_assistant_reply(
                    session_id=session_id, node_key=node_key, kind="ask", content=_reply, db=db)
            except Exception:
                logger.warning("setup_offer_capture_failed sid=%s", session_id)
            handled["v"] = "setup_recipe_started"
            return
        if assist_policy.looks_like_decline(text_):
            await _es.clear_pending_setup_offer(session_id=session_id, db=db)
            _reply = "Understood — not opening it. It stays available under **Capabilities** whenever you want it."
            yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": _reply})
            try:
                await assist_agent.capture_assistant_reply(
                    session_id=session_id, node_key=node_key, kind="ask", content=_reply, db=db)
            except Exception:
                logger.warning("setup_offer_capture_failed sid=%s", session_id)
            handled["v"] = "setup_recipe_declined"
            return
        # anything else supersedes the offer — the operator has moved on
        await _es.clear_pending_setup_offer(session_id=session_id, db=db)
    _recipe = _es.match_recipe(text_)
    if _recipe is not None:
        yield _ev(ASSIST_TURN_ROUTED, {"action": "ask", "override": "engine_capability"})
        _status, _detail, _job = "off", "", None
        try:
            for _r in await _es.list_recipes(db):
                if _r["id"] == _recipe.id:
                    _status, _detail = _r["status"], _r["status_detail"]
                    _job = {"job_id": _r["job_id"], "status": _r["job_status"]} if _r.get("job_id") else None
        except Exception as exc:
            logger.warning("engine_capability_status_failed recipe=%s err=%r", _recipe.id, exc)
        _reply = _es.capability_answer(_recipe, status=_status, detail=_detail, job=_job)
        if _status != "on":
            await _es.stage_setup_offer(session_id=session_id, recipe_id=_recipe.id, db=db)
        logger.info("engine_capability_answered sid=%s recipe=%s status=%s", session_id, _recipe.id, _status)
        yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": _reply})
        try:
            await assist_agent.capture_assistant_reply(
                session_id=session_id, node_key=node_key, kind="ask", content=_reply, db=db)
        except Exception:
            logger.warning("engine_capability_capture_failed sid=%s", session_id)
        handled["v"] = "engine_capability"
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
    except Exception as exc:
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
    # §17.1053b — add_step IS the plan change; a reshape tag on it must
    # not divert the turn to the note path (live: "add a step for this"
    # routed add_step + reshape → filed as a note, nothing added).
    if confident and (action == "note" or (impact == "reshape" and action != "add_step")):
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
        elsewhere = False   # §17.1101 — the paste completed other pending step(s)
        async for e in _submit(session_id, d, text_, nk, history, db):
            if e[0] == ASSIST_STEP_OUTCOME:
                if e[1].get("status") == "committed":
                    done = True
                elif e[1].get("status") == "committed_elsewhere":
                    elsewhere = True
                elif e[1].get("status") in ("step_incomplete", "verification_failed",
                                            "step_unverified"):  # §17.1016
                    blocked_reason = e[1].get("verify_reason") or "the step's goal isn't met yet"
            yield e
        if done:
            await _clear_completion_confirm(session_id, db)
            async for e in _claim_and_guide(session_id, None, history, db,
                                            orient=False):
                yield e
        elif elsewhere:
            # §17.1101 — the matched steps are committed; re-present the step
            # in focus so the operator continues where they were.
            async for e in _claim_and_guide(session_id, nk, history, db, orient=True):
                yield e
        elif blocked_reason is not None:
            # §17.951 — OFFER the operator the commit on their word.
            # §17.890 already honours a BARE claim, but the common real
            # shape — evidence plus an assertion, or a long report ending
            # "all of that was downloaded" — takes the evidence path,
            # verifies `incomplete`, and the operator got another fix
            # instead of being asked. Staged BEFORE the fix flow so the
            # invitation leads; they still get the help underneath it if it
            # genuinely is not done.
            _offer_made = False
            # §17.1014 — if the operator just told us they cannot tell,
            # leading with "reply `confirm`" hands the question back to the
            # one person who has already said they cannot answer it. Live
            # (ADD3/T35, 2026-09-11 02:03–02:11): "i believe it is done but
            # am unsure" and "all i could do was add a security group i am
            # unsure where dmz came from" were each answered with the
            # confirm offer, twice, and the operator reported the engine
            # could not help them CHECK. The step's own `## Verify` section
            # is the answer to their actual question and is already stored.
            _check = ""
            if assist_policy.expresses_uncertainty(text_ or ""):
                try:
                    from app.modules import assist_guide  # deferred: cycle-safe
                    _check = await assist_guide.how_to_check_block(
                        session_id=session_id, node_key=nk, db=db)
                except Exception as exc:
                    logger.warning("how_to_check_failed sid=%s err=%r",
                                   session_id, exc)
            _confirm_offer_text = (
                (f"I couldn't verify this step myself — {blocked_reason}\n\n"
                 + _check + "\n\n"
                 "Once you can see the result, paste it and I'll take it "
                 "from there. **If you'd rather I take your word for it, "
                 "reply `confirm`** and I'll mark it complete and move on.")
                if _check else
                (f"I couldn't verify this step myself — {blocked_reason}\n\n"
                 "**If it IS done, reply `confirm`** and I'll mark it "
                 "complete on your word and move to the next step. You "
                 "know your machine; I only see what you paste.\n\n"
                 "If something is still outstanding, here's where I'd "
                 "look next:"))
            try:
                from app.modules import assist_notes
                await assist_notes.stage_completion_confirm(
                    session_id=session_id, node_key=nk,
                    reason=blocked_reason, db=db)
                yield _ev(ASSIST_ANSWER,
                          {"kind": "ask", "text": _confirm_offer_text})
            except Exception as exc:
                logger.warning("completion_confirm_offer_failed sid=%s err=%r",
                               session_id, exc)
            else:
                # §17.952 — the offer is a QUESTION awaiting an answer, so it
                # has to survive a reload like every other substantive reply
                # (§17.873). It did not: staging recorded THAT the engine
                # asked (session metadata), the transcript never recorded
                # WHAT it asked. The SPA renders the streamed bubble into
                # `ephemeralTail` only, and rebuilds from `assist_turns` on
                # every reload — so the invitation evaporated and the
                # operator was left reading a run of fixes, with no sign the
                # engine had ever offered to take their word. Live on
                # 2026-09-06: staged three times (T29 11:37, T29 11:40,
                # T31 12:09), present in ZERO of the session's 584 turns;
                # the operator gave up and forced T29 with the Done button.
                try:
                    await assist_agent.capture_assistant_reply(
                        session_id=session_id, node_key=nk, kind="ask",
                        content=_confirm_offer_text, db=db,
                    )
                except Exception:
                    logger.warning(
                        "completion_confirm_capture_failed sid=%s", session_id)
                _offer_made = True
            # §17.884 — a blocked submit must NEVER dead-end. Live incident:
            # the operator ran the discovery command the engine asked for,
            # pasted the ground truth back, the verifier (correctly) said
            # "step not complete" — and the turn ENDED, discarding the very
            # information the engine had requested. Continue into the fix
            # flow seeded with the evidence + the verifier's reason: the
            # pasted values are now provenance-legal grounding, so the next
            # command can use them directly.
            # §17.953 — the offer leads (for anyone reading top-down) and a
            # one-liner closes the reply, where the eye actually lands in a
            # chat pinned to its bottom. §17.1056 — that closer is the fix's
            # last line, not a third bubble.
            _nudge = ("↩︎ Or — if this step is in fact already done on "
                      "your machine, reply `confirm` and I'll mark it "
                      "complete and move to the next one.") if _offer_made else None
            async for e in _fix_flow(
                session_id, nk,
                (f"{text_}\n\n[Progress noted, but the step is not complete "
                 f"yet — verifier: {blocked_reason}] Continue from the "
                 "output above: use the concrete values it contains."),
                history, db,
                status_text="Good progress — the step isn't finished yet, so I'm working out your next move from what you just pasted…",
                trailer=_nudge,
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
        except Exception as exc:
            yield _ev(ASSIST_TURN_STATUS, {"text": f"Couldn't skip that step ({exc}). It stays open."})
        handled["v"] = "skip"
        return
    if confident and action in ("advance", "finalize"):
        # §17.886(#2) — run the tracker reconcile, then honor EVERY result
        # action (the old code matched only 'advanced', so 'finalized' and
        # 'added_step' re-guided the stale node).
        async for e in _track_then_continue(session_id, text_, nk, history, db):
            yield e
        handled["v"] = action
        return
    if confident and action == "pause":
        from app.routers.assist import assist_pause
        try:
            await assist_pause(session_id, db=db)
            yield _ev(ASSIST_TURN_STATUS, {"text": "⏸ Session paused — say \"resume\" whenever you're ready and we'll pick up exactly here."})
        except Exception as exc:
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
            # §17.1053 — name what was inserted (one step or a chain), so a
            # proposal of several fixes reads as several steps, not as the
            # first one silently swallowing the rest.
            _steps = (res or {}).get("steps") or []
            if len(_steps) > 1:
                _lines = "\n".join(
                    f"- **{st.get('node_key')}**: {st.get('title')}" for st in _steps)
                yield _ev(ASSIST_TURN_STATUS, {
                    "text": (f"➕ Added {len(_steps)} steps before **{nk}**, in order:\n"
                             f"{_lines}\nStarting with the first.")})
            elif _steps:
                yield _ev(ASSIST_TURN_STATUS, {
                    "text": (f"➕ Added a step: **{_steps[0].get('title')}** — we'll do "
                             f"this first, then return to **{nk}**.")})
            async for e in _claim_and_guide(session_id, new_nk, history, db, orient=False):
                yield e
        except Exception as exc:
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
        except Exception as exc:
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
            _env_res = await assist_set_env(
                session_id,
                AssistEnvInput(substitutions=subs or None, verbosity=verb),
                db=db,
            )
            yield _ev(ASSIST_TURN_STATUS, {"text": "Noted — environment updated."})
            async for e in _reconciliation_note(session_id, nk, _env_res, db):  # §17.1046
                yield e
        except Exception as exc:
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
            capture_reply=False,  # §17.1136 — persisted once, below, under the resolved key
        )
        answer = (res or {}).get("answer") or ""
        if answer.strip():
            yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": answer})
            try:  # §17.873 — durable transcript capture (dedupe-safe)
                await assist_agent.capture_assistant_reply(
                    session_id=session_id, node_key=(res or {}).get("node_key") or nk, kind="ask",
                    content=answer, db=db,
                )
            except Exception:
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
                capture_reply=False,  # §17.1136
            )
            answer = (res or {}).get("answer") or ""
            if answer.strip():
                yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": answer})
                try:  # §17.873 — durable transcript capture (dedupe-safe)
                    await assist_agent.capture_assistant_reply(
                        session_id=session_id, node_key=(res or {}).get("node_key") or nk, kind="ask",
                        content=answer, db=db,
                    )
                except Exception:
                    logger.warning("turn_loop_research_capture_failed sid=%s", session_id)
                handled["v"] = "research_fallback"
                return
        except Exception as exc:
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
    async for e in _reconciliation_note(session_id, nk, res, db):  # §17.1045
        yield e
    if res.get("replan_proposal"):
        yield _ev(ASSIST_REPLAN_PROPOSAL, {"proposal": res["replan_proposal"]})


async def _surface(session_id: str, d: dict, text_: str, nk, db) -> AsyncIterator[_Event]:
    """plan_impact=surface, actionable (§17.863): record + impact pass; only a
    concrete proposal surfaces — silence stays silent."""
    try:
        async for e in _note(session_id, d, text_, nk, db):
            if e[0] != ASSIST_TURN_STATUS:  # keep surface quiet unless material
                yield e
    except Exception as exc:
        logger.warning("turn_loop_surface_failed sid=%s err=%r", session_id, exc)



async def _clear_completion_confirm(session_id: str, db) -> None:
    """§17.951 — drop any staged confirmation. Called wherever a step actually
    moves, so a stale "confirm?" can never attach itself to a step the operator
    has since left."""
    try:
        from app.modules import assist_notes
        await assist_notes.clear_pending_completion_confirm(
            session_id=session_id, db=db)
    except Exception as exc:
        logger.warning("completion_confirm_clear_failed sid=%s err=%r", session_id, exc)


async def _recent_advance_message(session_id: str, node_key, db) -> str | None:
    """§17.950 — the operator's most recent words on this step, IF they carry an
    advancement signal.

    Returns None otherwise, which is the common case and keeps Guide me behaving
    exactly as it always has. Deliberately narrow:

      * only the LATEST operator turn on this step is considered — an
        advancement signal from earlier in a long troubleshooting thread has
        already been superseded by whatever came after it;
      * only `message`/`submit` kinds, never a `note`;
      * the signal is `assist_policy.has_advancement_signal`, the same §17.891
        gate the message path uses, so this cannot advance on anything the
        typed path would not.

    Fail-soft: any error returns None and Guide me re-guides.
    """
    if not node_key:
        return None
    try:
        row = (await db.execute(
            _sqltext("""
                SELECT content FROM assist_turns
                 WHERE session_id = :sid AND node_key = :nk
                   AND role = 'operator' AND kind IN ('message', 'submit')
                 ORDER BY created_at DESC, id DESC LIMIT 1
            """),
            {"sid": session_id, "nk": node_key},
        )).mappings().first()
        msg = (row or {}).get("content") or ""
        if msg.strip() and assist_policy.has_advancement_signal(msg):
            logger.info(
                "turn_loop_guide_advance_candidate sid=%s nk=%s msg=%r",
                session_id, node_key, msg[:120])
            return msg
    except Exception as exc:
        logger.warning("guide_advance_probe_failed sid=%s err=%r", session_id, exc)
    return None


async def _track_then_continue(session_id: str, text_: str, nk, history, db) -> AsyncIterator[_Event]:
    """§17.886(#2) — tracker reconcile honoring EVERY result action. A
    `finalize` decision reaches here too: the tracker's own verdict decides
    whether the plan is complete (its `finalized` action), so there is no
    separate flag to pass (§17.1059 — vulture found the one that was)."""
    from app.modules import assist_agent
    from app.routers.assist import AssistInterpretInput, assist_track
    tr = {}
    try:
        yield _ev(ASSIST_TURN_STATUS, {"text": "Checking step progress…"})
        tr = await assist_track(
            session_id, AssistInterpretInput(message=text_, node_key=nk, history=history),
            db=db,
        ) or {}
    except Exception as exc:
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
    res = None  # §17.1136 — the capture below reads the resolved key even when the helper raised
    try:
        res = await assist_agent.run_step_research(
            session_id=session_id, node_key=nk, question=question,
            history=history, db=db,
            capture_reply=False,  # §17.1136 — persisted once, below, under the resolved key
        )
        answer = (res or {}).get("answer") or ""
    except Exception as exc:
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
            session_id=session_id, node_key=(res or {}).get("node_key") or nk, kind="ask", content=answer, db=db,
        )
    except Exception:
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
        except Exception:
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
    except Exception as exc:
        logger.warning("turn_loop_blocked_note_failed sid=%s err=%r", session_id, exc)


async def _start_state_check(session_id: str, nk, db) -> AsyncIterator[_Event]:
    """§17.1050 — probe phase: one read-only script, staged as pending."""
    from app.modules import assist_agent, assist_state_check as _sc
    yield _ev(ASSIST_TURN_STATUS, {"text": "🩺 Working out what the plan believes about your system and how to check each part (this can take a minute)…"})
    # §17.1051 — a long session has dozens of claims → several model calls;
    # the browser must not sit on one status line for two minutes.
    progress_q: asyncio.Queue = asyncio.Queue()

    async def _progress(batch_i: int, batches: int, so_far: int) -> None:
        await progress_q.put(f"🩺 Building the checks… batch {batch_i} of {batches} ({so_far} so far)")

    task = asyncio.create_task(_sc.start_state_check(db=db, session_id=session_id, node_key=nk, on_progress=_progress))
    try:
        while not task.done():
            try:
                msg = await asyncio.wait_for(progress_q.get(), timeout=1.0)
                yield _ev(ASSIST_TURN_STATUS, {"text": msg})
            except asyncio.TimeoutError:
                continue
        res = task.result()
    except Exception as exc:
        yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": f"I couldn't start the state check ({exc}). Tell me in your own words what is and is not working."})
        return
    # §17.1077 — the opt-in local runner closes the loop: run the probes,
    # record them as the operator turn a paste would have been, judge.
    try:
        from app.modules import assist_local_runner as _lr
        _spec = await _lr.runner_spec(db)
    except Exception as exc:
        logger.warning("local_runner_lookup_failed sid=%s err=%r", session_id, exc)
        _spec = None
    if _spec is not None and res.get("probes"):
        yield _ev(ASSIST_TURN_STATUS, {"text": f"🩺 Running {len(res['probes'])} read-only checks through your local runner ({_spec.name})…"})
        _pq: asyncio.Queue = asyncio.Queue()
        async def _lp(i: int, n: int) -> None:
            await _pq.put(f"🩺 Local runner: {i} of {n} checks done…")
        _t = asyncio.create_task(_lr.run_probes(_spec, res["probes"], on_progress=_lp))
        while not _t.done():
            try:
                yield _ev(ASSIST_TURN_STATUS, {"text": await asyncio.wait_for(_pq.get(), timeout=1.0)})
            except asyncio.TimeoutError:
                continue
        while not _pq.empty():          # frames the fast tail of the loop left behind
            yield _ev(ASSIST_TURN_STATUS, {"text": _pq.get_nowait()})
        pasted, executed = _t.result()
        if executed:
            record = _lr.transcript_record(executed, pasted)
            _rnk = nk or res.get("node_key")
            try:
                await assist_agent.ingest_turn(session_id=session_id, role="operator", kind="message",
                                               content=record, node_key=_rnk, db=db)
            except Exception:
                logger.warning("local_runner_record_failed sid=%s", session_id)
            logger.warning("local_runner_executed sid=%s node_key=%s probes=%d ok=%d",
                           session_id, _rnk, len(executed), sum(1 for e in executed if e["ok"]))
            async for e in _resolve_state_check(session_id, nk, pasted, [], db):
                yield e
            return
        yield _ev(ASSIST_TURN_STATUS, {"text": "🩺 The local runner executed nothing — falling back to the paste."})
    # §17.1081 — the paste request is where the operator feels the cost; say
    # once, here, that the engine can carry it (no line when a runner is set).
    _msg = res["message"]
    if res.get("probes"):
        from app.modules.engine_setup import runner_nudge
        _msg = _msg + runner_nudge()
    yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": _msg})
    try:
        await assist_agent.capture_assistant_reply(
            session_id=session_id, node_key=nk, kind="ask", content=_msg, db=db)
    except Exception:
        logger.warning("state_check_capture_failed sid=%s", session_id)


async def _resolve_state_check(session_id: str, nk, pasted: str, history, db,
                               finish: bool = False) -> AsyncIterator[_Event]:
    """§17.1050 — judge phase: verdicts, retractions, staged repairs.
    §17.1138 — a partial paste keeps the check pending (the reply carries the
    next script); ``finish`` closes it with what was answered."""
    from app.modules import assist_agent, assist_state_check as _sc
    yield _ev(ASSIST_TURN_STATUS, {"text": "🩺 Reading the checks against what the plan believes…"})
    res = await _sc.resolve_state_check(db=db, session_id=session_id, pasted=pasted, finish=finish)
    if res.get("message"):
        yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": res["message"]})
        try:
            await assist_agent.capture_assistant_reply(
                session_id=session_id, node_key=nk, kind="ask", content=res["message"], db=db)
        except Exception:
            logger.warning("state_check_result_capture_failed sid=%s", session_id)
    if res.get("proposal"):
        yield _ev(ASSIST_REPLAN_PROPOSAL, {"proposal": res["proposal"]})


async def _reconcile_plan(session_id: str, node_key, db) -> AsyncIterator[_Event]:
    """§17.1104 — run the fact/system-map reconciliation and surface it: a note
    for the steps auto-completed (already done per a confirmed fact), and a
    plan-change proposal for the obsolete/duplicate steps to drop (confirmed)."""
    from app.modules import assist_agent
    try:
        res = await assist_agent.reconcile_plan_against_facts(session_id=session_id, db=db)
    except Exception as exc:
        logger.warning("plan_reconcile_failed sid=%s err=%r", session_id, exc)
        return
    comp = res.get("completed") or []
    if comp:
        lines = "\n".join(f"- ✅ **{c['node_key']}** — {c['title']}" for c in comp)
        msg = ("Reconciled against your confirmed facts — "
               f"{'this step was' if len(comp) == 1 else f'{len(comp)} steps were'} already done, "
               f"marked complete:\n\n{lines}")
        yield _ev(ASSIST_ANSWER, {"kind": "note", "text": msg})
        try:
            await assist_agent.capture_assistant_reply(
                session_id=session_id, node_key=node_key, kind="note", content=msg, db=db)
        except Exception:
            logger.warning("plan_reconcile_note_capture_failed sid=%s", session_id)
    prop = res.get("proposals")
    if prop and prop.get("proposals"):
        yield _ev(ASSIST_REPLAN_PROPOSAL, {"proposal": prop})


async def _fix_flow(session_id: str, nk, error_text: str, history, db,
                    *, status_text: str, trailer: str | None = None) -> AsyncIterator[_Event]:
    """§17.874/884 — the research-backed fix sequence, shared by the fix
    dispatch branch and the incomplete-submit continuation."""
    from app.modules import assist_agent
    # §17.1050 — after several fixes on one step, offer the state check BEFORE
    # yet another fix (never forced: the fix still follows).
    try:
        if settings.assist_state_check_enabled and nk:
            from app.modules import assist_state_check as _sc
            _streak, _ = await assist_agent._fix_failure_streak(session_id=session_id, node_key=nk, db=db)
            if _streak >= settings.assist_state_check_after_fixes and \
                    not await _sc.state_check_done_on_step(db=db, session_id=session_id, node_key=nk):
                _offer = _sc.offer_text(_streak)
                yield _ev(ASSIST_ANSWER, {"kind": "note", "text": _offer})
                logger.info("state_check_offered sid=%s node_key=%s streak=%d", session_id, nk, _streak)
    except Exception as exc:
        logger.warning("state_check_offer_failed sid=%s err=%r", session_id, exc)
    yield _ev(ASSIST_TURN_STATUS, {"text": status_text})
    fix = await assist_agent.run_step_fix(
        session_id=session_id, node_key=nk, error=error_text,
        history=history, research=True,
        capture_reply=False,   # §17.1099 — _fix_flow persists the final (trailer-augmented) copy below; run_step_fix must not also persist, or the fix appears twice
        db=db,
    )
    # §17.876 — honest, actionable fallback (never a silent dead end).
    fix_text = (fix or {}).get("fix") or (
        "I couldn't produce a fix this time — the model returned no "
        "usable answer after several attempts. This is a generation "
        "hiccup, not a verdict on your problem. Press Send again to "
        "retry (research is re-run fresh), or paste just the last "
        "~50 lines of the error output to tighten the context."
    )
    if (trailer or "").strip():
        # §17.1056 — the §17.953 closing nudge rides INSIDE the fix (its last
        # line) instead of a third bubble; same words, same place the eye
        # lands, one less message per blocked paste.
        fix_text = fix_text.rstrip() + "\n\n---\n" + trailer.strip()
    yield _ev(ASSIST_ANSWER, {"kind": "fix", "text": fix_text})
    try:  # §17.873 — answers must outlive the run row
        await assist_agent.capture_assistant_reply(
            session_id=session_id, node_key=nk, kind="fix",
            content=fix_text, db=db,
        )
    except Exception:
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
        except Exception as exc:
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
        # §17.1101 — the paste completed OTHER pending step(s) (the cursor had
        # drifted). Say which, durably, and let the branch re-guide this step.
        if (res or {}).get("status") == "committed_elsewhere":
            _mc = (res or {}).get("matched_commits") or []
            _lines = "\n".join(f"- ✅ **{m['node_key']}** — {m['title']}" for m in _mc)
            _msg = ("That output didn't match the step in focus, but it completes "
                    f"{'this step' if len(_mc) == 1 else f'{len(_mc)} steps'} elsewhere in your "
                    f"plan — marked done:\n\n{_lines}\n\nBack to the current step:")
            yield _ev(ASSIST_ANSWER, {"kind": "note", "text": _msg})
            try:
                from app.modules import assist_agent as _aa
                await _aa.capture_assistant_reply(
                    session_id=session_id, node_key=nk, kind="note", content=_msg, db=db)
            except Exception:
                logger.warning("evidence_match_note_capture_failed sid=%s", session_id)
        async for e in _reconciliation_note(session_id, nk, res, db):  # §17.1043
            yield e
        # §17.889(#3) — a deliberating decision step computed a needs-input
        # question and THREW IT AWAY (rendered as a bare toast). Surface +
        # capture it like any other engine answer.
        dm = (res or {}).get("decision_message") or ""
        if dm.strip() and (res or {}).get("status") == "deliberating":
            yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": dm})
            # §17.1136 (ledger D-4) — NOT captured here: `decision_message` is the
            # deliberation reply run_step_decision already persisted (kind
            # "deliberation") on the way through the submit endpoint. A second
            # capture under the turn's key was the helper-and-caller double persist.
    except Exception as exc:
        # §17.889(#11) — durable answer (status lines vanish at turn end) and an
        # actual continuation instead of a dangling "Continuing…".
        msg = (f"I recorded what you pasted, but the step wouldn't accept it as a "
               f"submission ({exc}). Here's where things stand instead:")
        yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": msg})
        try:
            async for e in _claim_and_guide(session_id, nk, history, db, orient=True):
                yield e
        except Exception:
            logger.warning("turn_loop_refused_submit_orient_failed sid=%s", session_id)


async def _reconciliation_note(session_id: str, nk, res, db) -> AsyncIterator[_Event]:
    """§17.1043 — when a committed step's confirmed fix was applied to the
    plan, say so in the transcript: what changed, in which steps."""
    from app.modules.plan_reconcile import render_note
    rec = (res or {}).get("reconciliation") or {}
    note = render_note(rec)
    if note:
        yield _ev(ASSIST_ANSWER, {"kind": "note", "text": note})
    if rec.get("replan_proposal"):  # §17.1044/1048 — proposals the operator confirms
        yield _ev(ASSIST_REPLAN_PROPOSAL, {"proposal": rec["replan_proposal"]})
    if not note:
        return
    try:
        from app.modules import assist_agent as _aa
        await _aa.capture_assistant_reply(
            session_id=session_id, node_key=nk, kind="note", content=note, db=db)
    except Exception:
        logger.warning("plan_reconcile_note_capture_failed sid=%s", session_id)


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
        except Exception as exc:
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
                # §17.955 — same class as §17.952: the single most consequential
                # "you are done" the engine ever emits, and it was streamed into
                # `ephemeralTail` only. On any reload the transcript ended on the
                # last fix, so the one message confirming the project was
                # finished simply was not there.
                _done_text = ("🎉 **Every step in this plan is done — the project is complete.** "
                              "The deliverable has been compiled: open the job's **Output** tab to read it, "
                              "or the **Plan** tab to review what changed along the way. Nothing further "
                              "is waiting on you here.")
                yield _ev(ASSIST_ANSWER, {"kind": "ask", "text": _done_text})
                try:
                    await assist_agent.capture_assistant_reply(
                        session_id=session_id, node_key=None, kind="ask",
                        content=_done_text, db=db,
                    )
                except Exception:
                    logger.warning("project_complete_capture_failed sid=%s",
                                   session_id)
            elif st == "paused":
                yield _ev(ASSIST_TURN_STATUS, {"text": "⏸ This session is paused — say \"resume\" to pick up where you left off."})
            else:
                try:
                    from app.modules.assist_notes import get_pending_replan
                    pend = await get_pending_replan(session_id=session_id, db=db)
                except Exception:
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
