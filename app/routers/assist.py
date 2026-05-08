"""Assistant Mode routes.

Mounted in `app/main.py` via `app.include_router(assist_router)`.
All routes inherit the global `Depends(require_api_key)` via the
FastAPI app dependencies — no per-route auth needed.

See app/modules/assist_agent.py for the underlying state machine and
OVERVIEW.md §9 ("Assist Mode") for the design.
"""
from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from starlette.responses import StreamingResponse

from app.database import get_db
from app.modules import assist_agent, assist_session_map

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


# ── Per-chat session map ─────────────────────────────────────────────
# Path scoped under `/assist/_chatmap/` to avoid colliding with
# `/assist/{session_id}/...`. The `_` prefix marks this as pipeline
# UX state, not part of the assist-session lifecycle.


@router.put("/assist/_chatmap/{chat_id}")
async def assist_chatmap_put(chat_id: str, body: AssistChatMapInput):
    await assist_session_map.remember(
        chat_id, session_id=body.session_id, last_node_key=body.last_node_key,
    )
    return {"chat_id": chat_id, "stored": True}


@router.get("/assist/_chatmap/{chat_id}")
async def assist_chatmap_get(chat_id: str):
    entry = await assist_session_map.recall(chat_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"no chat map for {chat_id}")
    return {"chat_id": chat_id, **entry}


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
    return step


@router.post("/assist/{session_id}/submit")
async def assist_submit(session_id: str, body: AssistSubmitInput, db=Depends(get_db)):
    try:
        return await assist_agent.submit_step(
            session_id=session_id,
            node_key=body.node_key,
            evidence=body.output,
            evidence_kind=body.evidence_kind,
            evidence_meta=body.evidence_meta,
            action=body.action,
            friction_note=body.friction_note,
            db=db,
        )
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

    from app.main import _sse_with_disconnect_watch  # avoid circular import at module-top

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
