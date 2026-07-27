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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import text
from starlette.responses import StreamingResponse

from app.config import settings
from app.database import get_db
from app.modules import assist_agent, assist_session_map

logger = logging.getLogger("scaffold")

router = APIRouter(tags=["Assist"])


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


class AssistResearchInput(BaseModel):
    question: str
    node_key: Optional[str] = None


class AssistFixInput(BaseModel):
    error: str = Field(description="The error / what went wrong while doing the step.")
    node_key: Optional[str] = None


class AssistInterpretInput(BaseModel):
    message: str = Field(description="The operator's plain-language message.")
    node_key: Optional[str] = Field(
        default=None, description="Defaults to the session's current step."
    )


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
async def assist_chatmap_put(chat_id: str, body: AssistChatMapInput, db=Depends(get_db)):
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
async def assist_start(body: AssistStartInput, db=Depends(get_db)):
    try:
        return await assist_agent.start_assist_session(
            job_id=body.job_id,
            handoff_policy=body.handoff_policy,
            replan_policy=body.replan_policy,
            db=db,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


# §17.626 — declared BEFORE `/assist/{session_id}` so the literal path wins the
# route match (FastAPI matches in declaration order; otherwise `candidates`
# binds to `{session_id}`).
@router.get("/assist/candidates")
async def assist_candidates(db=Depends(get_db)):
    """Jobs a user could step through in Assist Mode (natural-language start)."""
    return {"candidates": await assist_agent.list_assist_candidates(db=db)}


@router.get("/assist/{session_id}")
async def assist_get_session(session_id: str, db=Depends(get_db)):
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
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
    return step


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
                research=body.research, force=body.force, db=db,
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
    try:
        return await assist_agent.run_step_fix(
            session_id=session_id,
            node_key=body.node_key,
            error=body.error,
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
        session_id=session_id, message=body.message, node_key=body.node_key, db=db,
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


@router.post("/assist/{session_id}/submit")
async def assist_submit(session_id: str, body: AssistSubmitInput, db=Depends(get_db)):
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

    verdict = None
    if body.action == "submit" and settings.assist_verify_on_submit:
        verdict = await assist_agent.verify_submit_outcome(
            session_id=session_id, node_key=body.node_key, evidence=body.output, db=db,
        )
        if (verdict and verdict.get("outcome") == "failed"
                and settings.assist_block_on_failed_verify):
            # Hard-block: do NOT commit — the step stays 'presented' (claimable)
            # for a clean re-submit. Log the blocker to the friction trail.
            await assist_agent.record_friction(
                session_id=session_id, node_key=body.node_key,
                note=f"verify-blocked: {verdict.get('reason', '')}", db=db,
            )
            return {
                "session_id": session_id,
                "node_key": body.node_key,
                "status": "verification_failed",
                "committed": False,
                "no_op": False,
                "next_node_key": None,
                "success_verdict": verdict,
                "mirror_divergence": False,
            }
    try:
        result = await assist_agent.submit_step(
            session_id=session_id,
            node_key=body.node_key,
            evidence=body.output,
            evidence_kind=body.evidence_kind,
            evidence_meta=body.evidence_meta,
            action=body.action,
            friction_note=body.friction_note,
            db=db,
        )
        if verdict is not None and isinstance(result, dict):
            result["success_verdict"] = verdict
        # §17.490 — learn the concrete values the operator used for this step's
        # placeholders so later walkthroughs are concrete. Best-effort; only
        # fires on a real commit and when the step's guidance had placeholders.
        # §17.644 — but NOT when the verifier judged the evidence a failure /
        # unrelated to the step: that evidence is about a different step (or is
        # noise), so learning from it produces garbage substitutions (e.g.
        # STORAGE=4TB scraped from "the 4TB drive is partitioned") that then
        # propagate into later steps' placeholders. `unclear`/`succeeded`/None
        # still learn — only a definite `failed` verdict suppresses it.
        _verdict_failed = bool(verdict) and verdict.get("outcome") == "failed"
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
    note = await assist_agent.record_note(
        session_id=session_id, text_=body.text, kind=body.kind,
        node_key=body.node_key, db=db,
    )
    if note is None:
        raise HTTPException(status_code=409, detail="empty note text")
    return {"recorded": True, "session_id": session_id, "note": note}


@router.get("/assist/{session_id}/notes")
async def assist_notes_list(session_id: str, db=Depends(get_db)):
    sess = await assist_agent.get_session(session_id=session_id, db=db)
    if not sess:
        raise HTTPException(status_code=404, detail=f"assist session not found: {session_id}")
    notes = await assist_agent.list_notes(session_id=session_id, db=db)
    return {"session_id": session_id, "notes": notes}
