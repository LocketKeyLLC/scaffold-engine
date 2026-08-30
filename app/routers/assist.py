"""Assistant Mode routes.

Mounted in `app/main.py` via `app.include_router(assist_router)`.
All routes inherit the global `Depends(require_api_key)` via the
FastAPI app dependencies — no per-route auth needed.

See app/modules/assist_agent.py for the underlying state machine and
OVERVIEW.md §9 ("Assist Mode") for the design.
"""
from __future__ import annotations

import logging
from typing import Literal, Optional
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from starlette.responses import StreamingResponse

from app.authz import (
    Principal,
    assert_visible,
    assert_visible_by_query,
    get_principal,
)
from app.config import settings
from app.database import get_db
from app.modules import assist_agent, assist_session_map

logger = logging.getLogger("scaffold")


# §17.810 — an assist_session has no owner column of its own; it derives from
# its parent job (assist_sessions.job_id → jobs.owner). This router-level
# dependency gates EVERY session-scoped route ({session_id} path param) in one
# place: a non-admin who isn't the parent job's owner gets a 404, identical to a
# missing session. Routes without a session_id path param (/assist/start,
# /assist/candidates, /assist/_chatmap/*) short-circuit here and enforce their
# own ownership where relevant. Admin / single-user installs no-op (no query).
async def _require_assist_session_visible(
    request: Request,
    db=Depends(get_db),
    principal: Principal = Depends(get_principal),
) -> None:
    if principal.is_admin:
        return
    sid = request.path_params.get("session_id")
    if not sid:
        return
    # A malformed session id can't own anything; leave it to the handler's own
    # not-found path rather than 500 on an invalid-UUID comparison.
    try:
        UUID(str(sid))
    except (ValueError, AttributeError, TypeError):
        return
    await assert_visible_by_query(
        db, principal,
        "SELECT j.owner FROM assist_sessions s JOIN jobs j ON j.id = s.job_id "
        "WHERE s.id = :sid",
        {"sid": str(sid)},
        detail=f"assist session not found: {sid}",
    )


router = APIRouter(
    tags=["Assist"],
    dependencies=[Depends(_require_assist_session_visible)],
)


class AssistStartInput(BaseModel):
    job_id: str
    handoff_policy: Literal["manual", "auto_on_skip", "auto_all_remaining"] = "manual"
    replan_policy: Literal["context_only", "selective", "full", "disabled"] = "context_only"


class AssistSubmitInput(BaseModel):
    node_key: str
    output: str = Field(default="", description="Human-supplied evidence (required for action='submit')")
    evidence_kind: Literal[
        "text", "command_output", "file_diff", "screenshot_ref", "url", "none"
    ] = "text"
    evidence_meta: dict = Field(default_factory=dict)
    action: Literal["submit", "skip"] = "submit"
    friction_note: Optional[str] = None
    history: list[dict] = Field(
        default_factory=list,
        description=(
            "§17.689 — recent conversation turns [{role, content}] so a DECISION "
            "step's concrete artifact can be assembled across turns before commit."
        ),
    )


class AssistHandoffInput(BaseModel):
    node_key: str
    mode: Literal["single", "all_remaining"] = "single"


class AssistFrictionInput(BaseModel):
    node_key: str
    note: str


class AssistChatMapInput(BaseModel):
    session_id: str
    last_node_key: Optional[str] = None


class AssistNoteInput(BaseModel):
    text: str = Field(description="The requirement / constraint / preference / decision to remember.")
    kind: Literal["note", "addition", "decision", "constraint", "preference"] = "note"
    node_key: Optional[str] = Field(
        default=None, description="The step it was raised on, if any (context only)."
    )


class AssistReplanDecisionInput(BaseModel):
    # §17.677 — resolve a pending note-triggered plan-fix proposal.
    decision: Literal["apply", "discard"] = "apply"


class AssistGuideInput(BaseModel):
    node_key: Optional[str] = Field(
        default=None, description="Defaults to the session's current step."
    )
    refine: Optional[str] = Field(
        default=None, description="Refinement hint, e.g. 'redo for macOS'."
    )
    research: Optional[bool] = Field(
        default=None, description="Override assist_guide_research for this call."
    )
    force: bool = Field(
        default=True, description="Regenerate even if a cached walkthrough exists."
    )
    history: list[dict] = Field(
        default_factory=list,
        description=(
            "§17.687 — recent conversation turns [{role, content}] (oldest "
            "first, current message excluded) so the walkthrough resolves "
            "references back to what was just discussed."
        ),
    )


class AssistResearchInput(BaseModel):
    question: str
    node_key: Optional[str] = None
    history: list[dict] = Field(
        default_factory=list,
        description="§17.687 — recent conversation turns [{role, content}].",
    )


class AssistFixInput(BaseModel):
    error: str = Field(description="The error / what went wrong while doing the step.")
    node_key: Optional[str] = None
    history: list[dict] = Field(
        default_factory=list,
        description="§17.687 — recent conversation turns [{role, content}].",
    )


class AssistAddStepInput(BaseModel):
    request: str = Field(
        description="What the new step should accomplish (the operator's request)."
    )
    before_node_key: Optional[str] = Field(
        default=None,
        description="Insert before this step (defaults to the current step).",
    )


class AssistInterpretInput(BaseModel):
    message: str = Field(description="The operator's plain-language message.")
    node_key: Optional[str] = Field(
        default=None, description="Defaults to the session's current step."
    )
    history: list[dict] = Field(
        default_factory=list,
        description="§17.687 — recent conversation turns [{role, content}].",
    )


class AssistTurnInput(BaseModel):
    # §17.710a — a raw turn for the append-only transcript. Captured
    # unconditionally by the pipeline for every chat message, before routing.
    role: str = Field(default="operator", description="'operator' | 'engine'.")
    kind: str = Field(
        default="message",
        description="submit | message | skip | note | guidance | decision.",
    )
    content: str = Field(default="", description="The raw turn text.")
    node_key: Optional[str] = Field(default=None)
    evidence_kind: Optional[str] = Field(default=None)


class AssistEnvInput(BaseModel):
    profile: Optional[str] = Field(
        default=None, description="Free-text environment profile (OS, shell, package manager)."
    )
    substitutions: dict = Field(
        default_factory=dict, description="Concrete value map, e.g. {HOST_IP: 10.0.0.5}."
    )
    verbosity: Optional[str] = Field(
        default=None, description="Walkthrough verbosity: terse | normal | detailed."
    )


# ── Per-chat session map ─────────────────────────────────────────────
# Path scoped under `/assist/_chatmap/` to avoid colliding with
# `/assist/{session_id}/...`. The `_` prefix marks this as pipeline
# UX state, not part of the assist-session lifecycle.


@router.put("/assist/_chatmap/{chat_id}")
async def assist_chatmap_put(
    chat_id: str, body: AssistChatMapInput, db=Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    # §17.854 (audit C7) — the router-level visibility guard keys on the
    # `session_id` PATH param; this route's session id is in the BODY, so the
    # guard no-ops here. Without this, a non-admin scoped key could bind ANY
    # session_id to a chat it controls (and the GET below would then leak that
    # session's status). Check the body's session_id explicitly. Admin /
    # single-user no-op, matching _require_assist_session_visible.
    if not principal.is_admin:
        try:
            UUID(str(body.session_id))
            await assert_visible_by_query(
                db, principal,
                "SELECT j.owner FROM assist_sessions s JOIN jobs j "
                "ON j.id = s.job_id WHERE s.id = :sid",
                {"sid": str(body.session_id)},
                detail=f"assist session not found: {body.session_id}",
            )
        except (ValueError, AttributeError, TypeError):
            # Malformed session id can't own anything → let remember() handle it.
            pass
    await assist_session_map.remember(
        chat_id, session_id=body.session_id, last_node_key=body.last_node_key,
    )
    # §17.538 — persist the link durably on the session row so it survives
    # Redis LRU eviction (the chatmap key is tiny + rarely read → prime
    # eviction bait under embedding-cache memory pressure, which was silently
    # orphaning active assist sessions from their chat → §17.537 gate saw
    # nothing → back to triage). Best-effort: Redis is still set above, so a
    # DB hiccup degrades to the pre-§17.538 (Redis-only) behaviour, not a 500.
    try:
        await db.execute(
            text("UPDATE assist_sessions SET chat_id = :cid WHERE id = :sid"),
            {"cid": chat_id, "sid": body.session_id},
        )
        await db.commit()
    except Exception as exc:  # noqa: BLE001
        await db.rollback()
        logger.warning("chatmap durable-link write failed for %s: %s", chat_id, exc)
    return {"chat_id": chat_id, "stored": True}


@router.get("/assist/_chatmap/{chat_id}")
async def assist_chatmap_get(chat_id: str, db=Depends(get_db)):
    entry = await assist_session_map.recall(chat_id)
    if entry is None:
        # §17.538 — Redis miss (typically LRU eviction). Recover the durable
        # link from Postgres: the most-recently-active session bound to this
        # chat. Re-seed Redis so subsequent reads are fast again (self-heal).
        # Only `active` sessions are recoverable — a terminal session must NOT
        # capture plain chat back into assist.
        try:
            row = (await db.execute(
                text("""
                    SELECT id, current_node_key
                      FROM assist_sessions
                     WHERE chat_id = :cid AND status = 'active'
                     ORDER BY last_activity_at DESC
                     LIMIT 1
                """),
                {"cid": chat_id},
            )).mappings().first()
        except Exception as exc:  # noqa: BLE001 — recovery is best-effort
            logger.warning("chatmap PG recovery failed for %s: %s", chat_id, exc)
            row = None
        if row is None:
            raise HTTPException(status_code=404, detail=f"no chat map for {chat_id}")
        sid = str(row["id"])
        last_node_key = row["current_node_key"]
        await assist_session_map.remember(
            chat_id, session_id=sid, last_node_key=last_node_key,
        )
        return {
            "chat_id": chat_id, "session_id": sid,
            "last_node_key": last_node_key, "status": "active",
        }
    # §17.537 — Redis hit: surface the mapped session's live status so the
    # pipeline can decide whether plain chat routes INTO assist (active) or
    # falls back to triage (terminal/missing). Best-effort: a purged session
    # row yields status=None, which the pipeline treats as "don't auto-route".
    status = None
    sid = entry.get("session_id")
    if sid:
        try:
            row = (await db.execute(
                text("SELECT status FROM assist_sessions WHERE id = :sid"),
                {"sid": sid},
            )).mappings().first()
            status = row["status"] if row else None
        except Exception:  # noqa: BLE001 — status is advisory; never 500 the map
            status = None
    return {"chat_id": chat_id, **entry, "status": status}


@router.delete("/assist/_chatmap/{chat_id}")
async def assist_chatmap_delete(chat_id: str):
    await assist_session_map.forget(chat_id)
    return {"chat_id": chat_id, "cleared": True}


@router.post("/assist/start")
async def assist_start(
    body: AssistStartInput,
    db=Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    # §17.810 — a session can only be started on a job the caller owns.
    await assert_visible(
        db, principal, body.job_id,
        detail=f"job not found: {body.job_id}",
    )
    try:
        result = await assist_agent.start_assist_session(
            job_id=body.job_id,
            handoff_policy=body.handoff_policy,
            replan_policy=body.replan_policy,
            db=db,
        )
        # §17.761 — attach a WHERE-YOU-ARE orientation so the reconnect leads with
        # project context, not a raw step. Only for a real started session.
        if (isinstance(result, dict) and result.get("session_id")
                and not result.get("assist_unavailable")):
            orient = await assist_agent.build_reconnect_orientation(
                session_id=result["session_id"], db=db)
            if orient:
                result["orientation"] = orient
        return result
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# §17.626 — declared BEFORE `/assist/{session_id}` so the literal path wins the
# route match (FastAPI matches in declaration order; otherwise `candidates`
# binds to `{session_id}`).
@router.get("/assist/candidates")
async def assist_candidates(
    in_progress: bool = False,
    db=Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Jobs a user could step through in Assist Mode (natural-language start).

    §17.681 — ``in_progress=true`` excludes terminal (completed/cancelled) jobs.
    The automatic cross-chat continuity paths pass it so a "continue"/topic
    match can't silently re-open a finished or reaper-cancelled job; the
    explicit-redo default keeps the full re-openable list.

    §17.810 — a non-admin sees only their own jobs as candidates."""
    return {
        "candidates": await assist_agent.list_assist_candidates(
            db=db, in_progress=in_progress,
            owner=None if principal.is_admin else principal.identity,
        )
    }


@router.get("/assist/{session_id}")
async def assist_get_session(session_id: str, db=Depends(get_db)):
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    # §17.863 — expose the un-resolved §17.677 note-replan proposal so clients
    # can re-render its apply/discard card after a reload (it previously lived
    # only in session metadata; a proposal surfaced mid-conversation was LOST
    # to any navigation until the next note happened to stage a new one).
    try:
        sess["pending_replan"] = await assist_agent.get_pending_replan(
            session_id=session_id, db=db)
    except Exception:  # noqa: BLE001 — additive field, never break the session read
        sess["pending_replan"] = None
    return sess


@router.get("/assist/{session_id}/next")
async def assist_next(session_id: str, db=Depends(get_db)):
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    if sess["status"] in ("completed", "abandoned", "cancelled"):
        return {"status": sess["status"], "session_id": session_id, "node_key": None}
    step = await assist_agent.get_next_step(session_id=session_id, db=db)
    if step is None:
        # No claimable pending step — could mean (a) all steps terminal, or
        # (b) some are in flight (presented / awaiting_input). Echo the
        # rollup so the client can tell.
        sess2 = await assist_agent.get_session(session_id=session_id, db=db)
        return {
            "status": sess2["status"] if sess2 else "unknown",
            "session_id": session_id,
            "node_key": None,
            "step_counts": (sess2 or {}).get("step_counts", {}),
        }
    # §17.647 — carry the session step roll-up on the claimed step too, so the
    # pipeline can tell a first walkthrough from a later one and trim the
    # repetitive "how to report back" footer after the operator has done a step.
    # (committed/skipped/handed_off don't change during a claim, so the pre-claim
    # sess counts are accurate for that decision.)
    if isinstance(step, dict):
        step["step_counts"] = sess.get("step_counts", {})
        # §17.864 — verify the claimed step's premise against the current
        # facts ledger BEFORE the operator walks into it. Valve-gated +
        # fail-soft inside; additive field, old clients ignore it.
        from app.modules import assist_premise
        verdict = await assist_premise.check_step_premise(
            session_id=session_id, step=step, db=db,
        )
        if verdict:
            step["premise_check"] = verdict
    return step


class AssistMessageInput(BaseModel):
    """§17.868 — one operator turn for the server-side turn loop."""
    message: Optional[str] = Field(default=None, description="The operator's message (command='message').")
    command: Literal["message", "guide"] = Field(
        default="message",
        description="'message' runs the full loop; 'guide' goes straight to claim-and-guide.",
    )
    node_key: Optional[str] = Field(default=None, description="Defaults to the session's current step.")
    history: list[dict] = Field(default_factory=list)


@router.post("/assist/{session_id}/message")
async def assist_message(
    session_id: str, body: AssistMessageInput, request: Request, db=Depends(get_db),
):
    """§17.868 — the server-side turn loop: ONE stream owns capture → gates →
    decide → dispatch → claim/premise → guidance, with a status frame at every
    stage (no silent phases). Clients render events; they never sequence the
    loop — the §17.861–867 seam-failure class ends here. Validates up front so
    bad input is an HTTP error, not a half-open stream."""
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    if sess["status"] not in ("active", "paused"):
        raise HTTPException(status_code=409, detail=f"session status {sess['status']!r} cannot take a turn")

    from app.modules import assist_turn
    from app.utils.sse import _sse_with_disconnect_watch

    # §17.869 — DETACHED: the loop runs as a background task writing frames to
    # assist_turn_runs; this response only TAILS the row. A disconnect (reload,
    # impatient navigation) kills the tail, never the turn — reconnecting via
    # GET /message/active replays every frame and resumes live.
    run_id = await assist_turn.start_turn_run(
        session_id=session_id, message=body.message, command=body.command,
        node_key=body.node_key, history=body.history,
    )

    async def _gen():
        try:
            async for name, data in assist_turn.tail_turn_run(run_id):
                yield assist_agent._sse(name, data)
        except Exception as exc:  # surface tail errors as an SSE frame
            yield assist_agent._sse("error", {"detail": str(exc)})

    return StreamingResponse(
        _sse_with_disconnect_watch(request, _gen()),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@router.get("/assist/{session_id}/message/active")
async def assist_message_active(session_id: str, db=Depends(get_db)):
    """§17.869 — the session's still-running turn (if any), for resume-on-load."""
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    from app.modules import assist_turn
    run_id = await assist_turn.get_active_run(session_id)
    return {"session_id": session_id, "run_id": str(run_id) if run_id else None}


@router.get("/assist/{session_id}/message/{run_id}/tail")
async def assist_message_tail(
    session_id: str, run_id: str, request: Request, db=Depends(get_db),
):
    """§17.869 — (re)attach to a turn run: replays every frame from the start,
    then follows live until the run finishes."""
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    from app.modules import assist_turn
    from app.utils.sse import _sse_with_disconnect_watch

    async def _gen():
        try:
            async for name, data in assist_turn.tail_turn_run(run_id):
                yield assist_agent._sse(name, data)
        except Exception as exc:
            yield assist_agent._sse("error", {"detail": str(exc)})

    return StreamingResponse(
        _sse_with_disconnect_watch(request, _gen()),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@router.post("/assist/{session_id}/guide")
async def assist_guide(session_id: str, body: AssistGuideInput, db=Depends(get_db)):
    """Generate (or return cached) the human walkthrough for a step.

    Separate from `/next` on purpose: this can take 10-60s (a thinking-model
    call plus an optional research pre-pass), so it must not block the fast
    atomic claim. `force=true` (the default; `/assist guide`) regenerates;
    the auto-guide path calls with `force=false` to hit the cache.
    """
    try:
        return await assist_agent.generate_step_guidance(
            session_id=session_id,
            node_key=body.node_key,
            refine=body.refine,
            research=body.research,
            force=body.force,
            history=body.history,
            db=db,
        )
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=409, detail=msg)


@router.post("/assist/{session_id}/guide/stream")
async def assist_guide_stream(
    session_id: str, body: AssistGuideInput, request: Request, db=Depends(get_db),
):
    """§17.493 — streaming variant of `/guide`. SSE: one `assist_guide_delta`
    per content chunk, then a single `assist_guide_done` with status +
    guidance_meta. A cache hit streams the cached text as one delta + done.
    Validates the session up front so bad input is an HTTP error, not a
    half-open stream (mirrors `/handoff`)."""
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    if sess["status"] not in ("active", "paused"):
        raise HTTPException(status_code=409, detail=f"session status {sess['status']!r} cannot generate guidance")
    nk = body.node_key or sess.get("current_node_key")
    if not nk:
        raise HTTPException(status_code=409, detail="no node_key supplied and session has no current step")

    from app.utils.sse import _sse_with_disconnect_watch
    from app.sse_events import ASSIST_GUIDE_DELTA, ASSIST_GUIDE_DONE

    async def _gen():
        try:
            async for ev in assist_agent.generate_step_guidance_stream(
                session_id=session_id, node_key=body.node_key, refine=body.refine,
                research=body.research, force=body.force, history=body.history, db=db,
            ):
                if ev.get("type") == "delta":
                    yield assist_agent._sse(ASSIST_GUIDE_DELTA, {"text": ev["text"]})
                else:
                    yield assist_agent._sse(ASSIST_GUIDE_DONE, {
                        "status": ev.get("status"),
                        "node_key": nk,
                        "guidance_meta": ev.get("guidance_meta") or {},
                        "cached": ev.get("cached", False),
                    })
        except Exception as exc:  # surface mid-stream errors as an SSE error frame
            yield assist_agent._sse("error", {"detail": str(exc)})

    return StreamingResponse(
        _sse_with_disconnect_watch(request, _gen()),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@router.post("/assist/{session_id}/research")
async def assist_research(session_id: str, body: AssistResearchInput, db=Depends(get_db)):
    """Confirm an operator-supplied question via SearXNG/Milvus + a short
    cited synthesis. A side query — not persisted to the step's guidance."""
    try:
        return await assist_agent.run_step_research(
            session_id=session_id,
            node_key=body.node_key,
            question=body.question,
            history=body.history,
            db=db,
        )
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=409, detail=msg)


@router.post("/assist/{session_id}/fix")
async def assist_fix(session_id: str, body: AssistFixInput, db=Depends(get_db)):
    """Diagnose an operator-reported error on a step and return corrected steps."""
    # §17.710a/§17.812 — lossless capture of the operator's error report, BEFORE
    # diagnosis. Slash/CLI/SDK fixes never pass the pipeline's /turn capture, so
    # the reported error lived only as a truncated friction note — invisible to
    # the recap/transcript and never derived. NL-path fixes reach the funnel
    # twice (raw message via /turn, error text here); the transcript renderer
    # collapses identical rows and the derive funnel dedupes recent content.
    await assist_agent.ingest_turn(
        session_id=session_id, role="operator", kind="fix",
        content=body.error, node_key=body.node_key, db=db,
    )
    try:
        return await assist_agent.run_step_fix(
            session_id=session_id,
            node_key=body.node_key,
            error=body.error,
            history=body.history,
            db=db,
        )
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=409, detail=msg)


@router.post("/assist/{session_id}/interpret")
async def assist_interpret(session_id: str, body: AssistInterpretInput, db=Depends(get_db)):
    """§17.626 — classify a plain-language turn into an assist intent so the
    pipeline can route it (advance / skip / submit / fix / finalize / pause /
    question) without the operator typing a /assist subcommand. Fail-soft: an
    unresolvable step or classifier hiccup returns intent='question'."""
    return await assist_agent.classify_session_turn(
        session_id=session_id, message=body.message, node_key=body.node_key,
        history=body.history, db=db,
    )


@router.post("/assist/{session_id}/decide")
async def assist_decide_turn(session_id: str, body: AssistInterpretInput, db=Depends(get_db)):
    """§17.771 (Phase 1) — the UNIFIED assist decision: one context-rich call that
    subsumes classify + progress-tracker + reroute, returning a full Decision
    {action, evidence/error_text/query/note_*, plan_impact, suggestion,
    confidence, rationale}. Gated by `assist_unified_decision_enabled` (default
    off; Phase 1 uses it only in shadow). Fail-soft: returns a low-confidence
    `question` decision on any hiccup so the caller can fall back to the cascade."""
    if not settings.assist_unified_decision_enabled:
        raise HTTPException(status_code=404, detail="unified decision disabled")
    from app.modules import assist_decide
    return await assist_decide.decide_turn(
        session_id=session_id, message=body.message, node_key=body.node_key,
        history=body.history, db=db,
    )


@router.put("/assist/{session_id}/env")
async def assist_set_env(session_id: str, body: AssistEnvInput, db=Depends(get_db)):
    """Set the operator's environment so walkthroughs use concrete commands."""
    try:
        env = await assist_agent.set_environment(
            session_id=session_id,
            profile=body.profile,
            substitutions=body.substitutions,
            verbosity=body.verbosity,
            db=db,
        )
    except ValueError as exc:
        # not-found → 404; invalid verbosity → 409
        if "not found" in str(exc):
            raise HTTPException(status_code=404, detail=str(exc))
        raise HTTPException(status_code=409, detail=str(exc))
    return {"session_id": session_id, "environment": env}


@router.get("/assist/{session_id}/env")
async def assist_get_env(session_id: str, db=Depends(get_db)):
    env = await assist_agent.get_environment(session_id=session_id, db=db)
    if env is None:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    return {"session_id": session_id, "environment": env}


@router.get("/assist/{session_id}/checklist")
async def assist_get_checklist(session_id: str, db=Depends(get_db)):
    """§17.707 — the operator-input checklist (decisions to make + info to
    supply) for the session's plan, with live done/open status + values learned
    so far."""
    try:
        return await assist_agent.build_inputs_checklist(session_id=session_id, db=db)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@router.post("/assist/{session_id}/turn")
async def assist_record_turn(session_id: str, body: AssistTurnInput, db=Depends(get_db)):
    """§17.710a — record ONE raw turn to the append-only transcript. The pipeline
    calls this first for every chat message so capture is unconditional (never
    gated on how the message later classifies). No-op unless the unified-memory
    capture valve is on; always returns 200 (fail-soft) so it never blocks the
    conversation."""
    recorded = await assist_agent.ingest_turn(
        session_id=session_id, role=body.role, kind=body.kind, content=body.content,
        node_key=body.node_key, evidence_kind=body.evidence_kind, db=db,
    )
    # §17.715/§17.812 — the per-message derive now rides the capture funnel
    # itself (ingest_turn schedules it for operator turns), so every capture
    # site — this endpoint, /submit, /note, /fix — gets identical treatment.
    return {"session_id": session_id, "recorded": recorded}


@router.get("/assist/{session_id}/turns")
async def assist_list_turns(session_id: str, limit: int = 200, db=Depends(get_db)):
    """§17.710a — the session's raw transcript, oldest-first."""
    turns = await assist_agent.list_turns(session_id=session_id, limit=limit, db=db)
    return {"session_id": session_id, "turns": turns}


@router.post("/assist/{session_id}/submit")
async def assist_submit(session_id: str, body: AssistSubmitInput, db=Depends(get_db)):
    # §17.710a — lossless capture, BEFORE any verify/deliberate/commit branching.
    # Source of truth for ALL clients (slash / curl / NL), so a submit is
    # recorded even on paths that never call the pipeline's /turn. No-op unless
    # the capture valve is on; fail-soft.
    await assist_agent.ingest_turn(
        session_id=session_id, role="operator",
        kind=("skip" if body.action == "skip" else "submit"),
        content=body.output, node_key=body.node_key,
        evidence_kind=body.evidence_kind, db=db,
    )
    # §17.487 — success verification. Runs BEFORE submit_step (so the slow LLM
    # call never holds submit_step's row lock, and submit_step stays pure).
    # Only for action='submit'; verify_submit_outcome returns None unless the
    # step is genuinely claimable ('presented').
    # §17.621 (audit #20) — consume handoff_policy. On a SKIP with a non-manual
    # policy, delegate to the autonomous executor instead of leaving the step
    # skipped: auto_on_skip hands off THIS step (mode=single, then back to
    # assist); auto_all_remaining hands off the step + the rest of the DAG
    # (mode=all_remaining). The node is still 'pending'/'presented' here (the
    # skip hasn't been committed), so handoff_step can claim it. Runs as a
    # background task (own session) so /submit still returns JSON immediately.
    if body.action == "skip":
        _sess = await assist_agent.get_session(session_id=session_id, db=db)
        if _sess and _sess.get("status") == "active":
            _policy = _sess.get("handoff_policy", "manual")
            if _policy in ("auto_on_skip", "auto_all_remaining"):
                _mode = "all_remaining" if _policy == "auto_all_remaining" else "single"
                assist_agent.spawn_handoff_background(
                    session_id=session_id, node_key=body.node_key, mode=_mode,
                )
                return {
                    "session_id": session_id,
                    "node_key": body.node_key,
                    "status": "auto_handoff",
                    "committed": False,
                    "no_op": False,
                    "next_node_key": None,
                    "handoff_policy": _policy,
                    "handoff_mode": _mode,
                    "mirror_divergence": False,
                }

    # §17.703 — execution-environment monitor. Capture the operator's shell
    # context (`user@host` in ONE interactive shell) from THIS submit's evidence,
    # unconditionally and up front — BEFORE the verify block can early-return on a
    # failed verdict, and independent of the substitution-learning valve. This is
    # the fix for "the engine forgot I was operating through root@pve": the old
    # §17.701 capture only ran inside learn_from_submit, i.e. only on a committed,
    # non-failed submit with learning enabled, so an error/failed paste (which
    # still carries the real prompt) never recorded the context. Fail-soft.
    captured_ctx = await assist_agent.capture_execution_context(
        session_id=session_id, evidence=body.output, db=db,
    )

    # §17.689 — decision deliberation. A decision node's concrete artifact is
    # assembled ACROSS turns: a partial answer must NOT terminally commit. Runs
    # before verify/submit; returns None (→ plain single-turn commit) unless this
    # is a claimable decision step and the model produced a usable result.
    deliberation = None
    if body.action == "submit" and settings.assist_decision_deliberation_enabled:
        deliberation = await assist_agent.run_step_decision(
            session_id=session_id, node_key=body.node_key,
            message=body.output, history=body.history, db=db,
        )
    if deliberation is not None and deliberation["status"] == "needs_input":
        # Do NOT commit — keep the step open and hand back the proposal / the
        # still-missing items so the operator can continue on the next turn.
        return {
            "session_id": session_id,
            "node_key": body.node_key,
            "status": "deliberating",
            "committed": False,
            "no_op": False,
            "next_node_key": None,
            "decision_message": deliberation["message"],
            "collect_kind": deliberation.get("collect_kind"),
            "mirror_divergence": False,
        }

    verdict = None
    # §17.689 — a resolved decision commits the concrete artifact the engine
    # assembled (not the operator's "looks good"); deliberation replaces the
    # generic success verify for that path.
    commit_evidence = body.output
    decision_message = None
    decision_kind = None
    if deliberation is not None and deliberation["status"] == "resolved":
        commit_evidence = deliberation["decision_record"]
        decision_message = deliberation.get("message") or None
        decision_kind = deliberation.get("collect_kind")
    elif body.action == "submit" and settings.assist_verify_on_submit:
        verdict = await assist_agent.verify_submit_outcome(
            session_id=session_id, node_key=body.node_key, evidence=body.output, db=db,
        )
        _v_outcome = verdict.get("outcome") if verdict else None
        # §17.731 — block a commit that would mark the step done when the
        # evidence shows either a clear failure OR the step's deliverable isn't
        # actually done yet ('incomplete' — e.g. the OS installer was only
        # downloaded / is at its boot menu). Each is independently valve-gated;
        # the step stays 'presented' so the operator finishes it (or /assist
        # skip to override a false block).
        _blocked = (
            (_v_outcome == "failed" and settings.assist_block_on_failed_verify)
            or (_v_outcome == "incomplete" and settings.assist_block_on_incomplete_verify)
        )
        if _blocked:
            # Hard-block: do NOT commit — the step stays 'presented' (claimable)
            # for a clean re-submit. Log the blocker to the friction trail.
            await assist_agent.record_friction(
                session_id=session_id, node_key=body.node_key,
                note=f"verify-blocked ({_v_outcome}): {verdict.get('reason', '')}", db=db,
            )
            return {
                "session_id": session_id,
                "node_key": body.node_key,
                "status": ("step_incomplete" if _v_outcome == "incomplete"
                           else "verification_failed"),
                "committed": False,
                "no_op": False,
                "next_node_key": None,
                "success_verdict": verdict,
                "mirror_divergence": False,
                # §17.703 — even a blocked submit records the operator's shell
                # context; surface it so the re-submit's guidance stays anchored.
                "execution_context": captured_ctx,
            }
    # §17.771 — a goal-met-via-alternative commit: the step succeeded but via a
    # different method than the plan named, because a hardware/software constraint
    # ruled the named one out. The endpoint runs a dedicated constraint-adaptation
    # after commit (record the constraint + re-plan the pending steps that assumed
    # otherwise), so the plain divergence re-plan inside submit_step is suppressed.
    _via_alt = bool(verdict) and bool(verdict.get("goal_met_via_alternative")) \
        and bool((verdict.get("constraint") or "").strip())
    try:
        result = await assist_agent.submit_step(
            session_id=session_id,
            node_key=body.node_key,
            evidence=commit_evidence,
            evidence_kind=body.evidence_kind,
            evidence_meta=body.evidence_meta,
            action=body.action,
            friction_note=body.friction_note,
            # §17.708 — a failed-verdict submit skips divergence re-plan (a failed
            # command is a recover situation, not a plan divergence). §17.731 —
            # an 'incomplete' commit (blocking valve off) is likewise not a
            # divergence.
            verdict_failed=(bool(verdict)
                            and verdict.get("outcome") in ("failed", "incomplete")),
            skip_divergence_replan=_via_alt,
            db=db,
        )
        if verdict is not None and isinstance(result, dict):
            result["success_verdict"] = verdict
        # §17.771 — ADAPT THE PLAN TO REALITY on a goal-met-via-alternative commit:
        # record the constraint durably + re-plan the pending steps that assumed
        # the impossible method. Fail-soft; surfaced via result['adaptation'].
        if (_via_alt and isinstance(result, dict)
                and result.get("status") == "committed"):
            try:
                adaptation = await assist_agent.adapt_step_to_constraint(
                    session_id=session_id, node_key=body.node_key,
                    constraint=verdict.get("constraint", ""), db=db,
                )
                if adaptation:
                    result["adaptation"] = adaptation
            except Exception:  # never fail a commit on the adaptation step
                logger.warning("assist_adapt_constraint_failed", exc_info=True)
        # §17.689 — surface the deliberation confirmation so the pipeline can
        # show what was recorded alongside the commit.
        if decision_message and isinstance(result, dict):
            result["decision_message"] = decision_message
        if decision_kind and isinstance(result, dict):
            result["collect_kind"] = decision_kind
        # §17.490 — learn the concrete values the operator used for this step's
        # placeholders so later walkthroughs are concrete. Best-effort; only
        # fires on a real commit and when the step's guidance had placeholders.
        # §17.644 — but NOT when the verifier judged the evidence a failure /
        # unrelated to the step: that evidence is about a different step (or is
        # noise), so learning from it produces garbage substitutions (e.g.
        # STORAGE=4TB scraped from "the 4TB drive is partitioned") that then
        # propagate into later steps' placeholders. `unclear`/`succeeded`/None
        # still learn — a definite `failed` OR `incomplete` (§17.731) verdict
        # suppresses it (incomplete evidence = setup only, wrong to learn from).
        _verdict_failed = bool(verdict) and verdict.get("outcome") in ("failed", "incomplete")
        if (settings.assist_learn_substitutions and body.action == "submit"
                and not _verdict_failed
                and isinstance(result, dict) and result.get("status") == "committed"):
            try:
                learned = await assist_agent.learn_from_submit(
                    session_id=session_id, node_key=body.node_key,
                    evidence=body.output, db=db,
                )
                if learned:
                    result["learned_substitutions"] = learned
            except Exception:  # never fail a submit on the learn step
                pass
        # §17.710c — warn-only grounding gate. BEFORE capturing this submit's own
        # facts, check the result against prior memory; a contradiction (e.g. a
        # decision assuming a fresh host when memory says an existing one)
        # surfaces a non-blocking warning. No-op unless the grounding valve is on.
        if (body.action == "submit"
                and isinstance(result, dict) and result.get("status") == "committed"):
            try:
                grounding = await assist_agent.check_submit_grounding(
                    session_id=session_id, node_key=body.node_key,
                    evidence=body.output, db=db,
                )
                if grounding:
                    result["grounding_warning"] = grounding
            except Exception:  # never fail a submit on the grounding gate
                pass
        # §17.709 — distill durable facts about the operator's system from this
        # submit into the session facts ledger (the retention layer placeholder
        # learning misses — an audit/inventory step has real state but no
        # placeholders). Runs even on a FAILED verdict: a failed audit
        # ("Connection refused") is itself a fact ("state unverified"), and
        # capturing it is what stops the next decision assuming a fresh system.
        if (settings.assist_capture_facts_enabled and body.action == "submit"
                and isinstance(result, dict) and result.get("status") == "committed"):
            try:
                facts = await assist_agent.capture_session_facts(
                    session_id=session_id, node_key=body.node_key,
                    evidence=body.output, db=db,
                )
                if facts:
                    result["captured_facts"] = facts
            except Exception:  # never fail a submit on the facts step
                pass
        # §17.703 — surface a newly-captured / switched shell context so the
        # pipeline can confirm it ("noted: you're on root@pve").
        if captured_ctx and isinstance(result, dict):
            result["execution_context"] = captured_ctx
        return result
    except ValueError as exc:
        msg = str(exc)
        if "not found" in msg:
            raise HTTPException(status_code=404, detail=msg)
        if msg.startswith("must_claim_first:"):
            raise HTTPException(
                status_code=409,
                detail={"error_code": "must_claim_first", "message": msg.split(": ", 1)[1]},
            )
        raise HTTPException(status_code=409, detail=msg)


@router.post("/assist/{session_id}/handoff")
async def assist_handoff(session_id: str, body: AssistHandoffInput, request: Request, db=Depends(get_db)):
    # Validate session/node before opening the SSE stream — caller gets a
    # proper HTTP error instead of a half-empty stream.
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    if sess["status"] != "active":
        raise HTTPException(status_code=409, detail=f"session status {sess['status']!r} cannot handoff")

    from app.utils.sse import _sse_with_disconnect_watch  # moved from app.main (§17.540)

    source = assist_agent.handoff_step(
        session_id=session_id,
        node_key=body.node_key,
        mode=body.mode,
        db=db,
    )
    return StreamingResponse(
        _sse_with_disconnect_watch(request, source),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@router.post("/assist/{session_id}/pause")
async def assist_pause(session_id: str, db=Depends(get_db)):
    try:
        return await assist_agent.pause_session(session_id=session_id, db=db)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/assist/{session_id}/resume")
async def assist_resume(session_id: str, db=Depends(get_db)):
    try:
        return await assist_agent.resume_session(session_id=session_id, db=db)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.delete("/assist/{session_id}")
async def assist_abandon(session_id: str, db=Depends(get_db)):
    try:
        return await assist_agent.abandon_session(session_id=session_id, db=db)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@router.post("/assist/{session_id}/friction")
async def assist_friction(session_id: str, body: AssistFrictionInput, db=Depends(get_db)):
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    await assist_agent.record_friction(
        session_id=session_id, node_key=body.node_key, note=body.note, db=db,
    )
    return {"recorded": True, "session_id": session_id, "node_key": body.node_key}


@router.get("/assist/{session_id}/friction")
async def assist_friction_list(session_id: str, db=Depends(get_db)):
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    notes = await assist_agent.list_friction(session_id=session_id, db=db)
    return {"session_id": session_id, "friction": notes}


# §17.654 — session notes & additions (project-scoped, feed-forward into guidance)
@router.post("/assist/{session_id}/note")
async def assist_note(session_id: str, body: AssistNoteInput, db=Depends(get_db)):
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    # §17.710a — capture the note as a raw turn too (source of truth for all
    # clients); no-op unless the capture valve is on.
    await assist_agent.ingest_turn(
        session_id=session_id, role="operator", kind="note",
        content=body.text, node_key=body.node_key, db=db,
    )
    note = await assist_agent.record_note(
        session_id=session_id, text_=body.text, kind=body.kind,
        node_key=body.node_key, db=db,
    )
    if note is None:
        raise HTTPException(status_code=409, detail="empty note text")
    # §17.677 — a plan-affecting note (constraint/decision/addition/preference)
    # gets an impact pass over the pending nodes; a non-empty proposal is
    # surfaced for the operator to confirm via POST /assist/{sid}/replan/apply.
    proposal = await assist_agent.assess_note_impact(
        session_id=session_id, note_kind=note["kind"], note_text=note["text"], db=db,
    )
    out = {"recorded": True, "session_id": session_id, "note": note}
    if proposal:
        out["replan_proposal"] = proposal
    # §17.755 — if THIS note declares a reset/rebuild (§17.714), retract the facts
    # about the now-abandoned system so the ledger stops dragging dead state into
    # later steps. Fail-soft; surfaces what was retracted so the operator sees it.
    from app.modules import assist_guide
    if assist_guide._operator_reset_intent([note]):
        swept = await assist_agent.sweep_superseded_facts(
            session_id=session_id, note_text=note["text"], db=db,
        )
        if swept.get("retracted"):
            out["retracted_facts"] = swept["retracted"]
    return out


@router.post("/assist/{session_id}/add_step")
async def assist_add_step(session_id: str, body: AssistAddStepInput, db=Depends(get_db)):
    """§17.736 — insert a new guided step (a foundational task the plan didn't
    cover) to run before the current step, then point the session at it. The
    caller follows with GET /assist/{sid}/next to present its walkthrough."""
    if not (body.request or "").strip():
        raise HTTPException(status_code=422, detail="request text is empty")
    try:
        return await assist_agent.add_step(
            session_id=session_id, request=body.request,
            before_node_key=body.before_node_key, db=db,
        )
    except ValueError as exc:
        msg = str(exc)
        raise HTTPException(
            status_code=404 if "not found" in msg else 409, detail=msg
        )


@router.post("/assist/{session_id}/reroute")
async def assist_reroute(session_id: str, body: AssistInterpretInput, db=Depends(get_db)):
    """§17.693 — semantic pivot check for a substantive turn the classifier read
    as skip/question. Runs the §17.677 impact analyzer over the pending plan; if
    the message invalidates steps, records it as a decision note + stages a
    pending_replan and returns ``{has_impact: true, proposal}``. Otherwise
    ``{has_impact: false}`` — a pure dry run so the caller proceeds with the
    original intent. Reuses AssistInterpretInput ({message, node_key, history})."""
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    proposal = await assist_agent.detect_reroute(
        session_id=session_id, message=body.message, db=db,
    )
    return {"session_id": session_id, "has_impact": bool(proposal),
            "proposal": proposal}


async def _retire_step_mirrored(
    *, db, job_id: str, session_id: str, node_key: str, evidence: str | None = None,
) -> None:
    """§17.754/§17.852 — mark a tracker-verified step DONE on BOTH dag_nodes and
    assist_steps in one commit (mirror invariant §17.286).

    Was 'skipped' ("the safe terminal state") — but the tracker only retires a
    step it is CONFIDENT the operator completed, and 'skipped' erased that work
    from the completed-work digest: downstream guidance believed the early
    steps never happened and re-prescribed them (live symptom: "the engine
    appears to be repeating the first step"). Done-with-evidence is the truth;
    the operator's own words become the node output so digests/recaps carry
    what actually happened. The ⏩ Skip verb (deliberate skip, work NOT done)
    still writes 'skipped' via the submit path — the two are semantically
    different and now recorded differently."""
    note = "Completed by the operator in assist mode (progress-tracker verified, §17.754)."
    if (evidence or "").strip():
        note += f"\nOperator's account: {evidence.strip()[:600]}"
    await db.execute(
        text("UPDATE dag_nodes SET status='done', "
             "output_text=COALESCE(NULLIF(output_text,''), :n), "
             "completed_at=NOW(), updated_at=NOW() "
             "WHERE job_id=:jid AND node_key=:nk AND status NOT IN ('done','skipped')"),
        {"n": note, "jid": job_id, "nk": node_key},
    )
    await db.execute(
        text("UPDATE assist_steps SET status='committed', committed_at=NOW(), "
             "updated_at=NOW() "
             "WHERE session_id=:sid AND node_key=:nk AND status NOT IN ('committed','skipped')"),
        {"sid": session_id, "nk": node_key},
    )
    # §17.880 — retiring a step must also MOVE the session pointer, exactly as
    # the submit-commit path does. The live incident: the tracker retired T14
    # here but current_node_key stayed on it, so every Guide/Done press
    # re-resolved the finished step ("this node is done" on repeat). Point at
    # the next claimable step (None when the plan is exhausted); the claim path
    # re-sets it idempotently when the operator walks in.
    nxt = await assist_agent._next_pending_node_key(session_id=session_id, db=db)
    await db.execute(
        text("UPDATE assist_sessions SET current_node_key = :nk, updated_at = NOW() "
             "WHERE id = :sid AND status IN ('active', 'paused')"),
        {"nk": nxt, "sid": session_id},
    )
    await db.commit()


@router.post("/assist/{session_id}/track")
async def assist_track(session_id: str, body: AssistInterpretInput, db=Depends(get_db)):
    """§17.754 — the progress-tracking agent. Reconcile the session pointer with
    where the operator actually is, and ACT to keep the plan in sync:

    - ``add_step`` (confident) — the operator raised a concrete sub-task no step
      covers, so insert a guided step (§17.736) and return it for the caller to
      present its walkthrough. This is the fix for "I asked for help with X and it
      repeated the current step."
    - ``advance`` / ``on_step`` — no mutation; the caller proceeds normally
      (advance is a hint that the current step looks done).

    Fail-soft: a disabled valve / flaky agent / low confidence all return
    ``{action: 'proceed'}`` so the caller falls through to its normal handling."""
    from app.config import settings
    from app.modules import assist_tracker

    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    # Capture the step the operator is leaving BEFORE add_step repoints the session.
    prior = (await db.execute(
        text("SELECT job_id, current_node_key FROM assist_sessions WHERE id = :sid"),
        {"sid": session_id},
    )).mappings().first()
    # §17.812 (audit I-3/M14) — thread the caller's node_key + history through:
    # cross-chat, the session pointer can sit on a DIFFERENT step than the one
    # the operator is discussing, and the retire below must hit the discussed
    # step. The tracker validates the key and reports which step it assessed.
    verdict = await assist_tracker.assess_progress(
        session_id=session_id, message=body.message, db=db,
        node_key=body.node_key, history=body.history,
    )
    out = {"session_id": session_id, "action": "proceed", "verdict": verdict}
    v = verdict.get("verdict")
    confident = float(verdict.get("confidence") or 0.0) >= settings.assist_tracker_confidence
    prior_nk = verdict.get("node_key") or (prior or {}).get("current_node_key")
    job_id = str((prior or {}).get("job_id"))
    if v == "add_step" and confident and (verdict.get("new_step_request") or "").strip():
        try:
            step = await assist_agent.add_step(
                session_id=session_id, request=verdict["new_step_request"], db=db,
            )
            out["action"] = "added_step"
            out["step"] = step
            # §17.754 — the tracker judged the prior step already complete (the
            # operator moved past it), so RETIRE it — else, after the new step, the
            # plan would loop the operator back to a step they've finished.
            if verdict.get("current_step_done") and prior_nk and prior_nk != (step or {}).get("node_key"):
                await _retire_step_mirrored(
                    db=db, job_id=job_id, session_id=session_id, node_key=prior_nk,
                    evidence=body.message)
                out["retired_prior_step"] = prior_nk
        except ValueError as exc:
            # A bad add (e.g. terminal session) must not 500 the tracker — fall
            # back to normal handling.
            out["action"] = "proceed"
            out["add_error"] = str(exc)
    elif v == "advance" and confident and verdict.get("current_step_done") and prior_nk:
        # §17.754 (#2) — the tracker is confident the current step is DONE and the
        # next work is an EXISTING pending step. Retire the current step (mirror
        # §17.286) so the next claimable step advances; the caller presents it.
        await _retire_step_mirrored(
            db=db, job_id=job_id, session_id=session_id, node_key=prior_nk,
            evidence=body.message)
        out["action"] = "advanced"
        out["retired_prior_step"] = prior_nk
        # §17.766 — retiring the step via the tracker must be able to FINALIZE the
        # session, exactly as a submit does (_maybe_finalize_session is otherwise
        # ONLY reached from submit_step). Without this, retiring the LAST
        # non-terminal step left the session 'active' with no claimable step and
        # the job never reached 'completed' (deliverable never compiled) — a
        # permanent stuck-at-completion that §17.765 (tracker-advance now reachable
        # from a how-to/help turn) made easy to hit. Idempotent: no-ops unless
        # every step is now terminal. On finalize, tell the caller so it renders
        # the completion instead of a confusing "no step ready".
        await assist_agent._maybe_finalize_session(session_id=session_id, db=db)
        if (await assist_agent.get_session(session_id=session_id, db=db) or {}).get(
            "status"
        ) == "completed":
            out["action"] = "finalized"
            out["session_finalized"] = True
    return out


@router.get("/assist/{session_id}/replan")
async def assist_replan_get(session_id: str, db=Depends(get_db)):
    """§17.677 — the session's un-resolved note-triggered plan-fix proposal.
    Returns ``{pending: <proposal>|null}`` (used by the pipeline confirm-gate)."""
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    pending = await assist_agent.get_pending_replan(session_id=session_id, db=db)
    return {"session_id": session_id, "pending": pending}


@router.post("/assist/{session_id}/replan/apply")
async def assist_replan_apply(
    session_id: str, body: AssistReplanDecisionInput, db=Depends(get_db),
):
    """§17.677 — apply or discard the session's pending note-triggered plan fix."""
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    result = await assist_agent.apply_pending_replan(
        session_id=session_id, decision=body.decision, db=db,
    )
    return {"session_id": session_id, **result}


@router.get("/assist/{session_id}/notes")
async def assist_notes_list(session_id: str, db=Depends(get_db)):
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    notes = await assist_agent.list_notes(session_id=session_id, db=db)
    return {"session_id": session_id, "notes": notes}
