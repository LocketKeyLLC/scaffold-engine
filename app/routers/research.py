"""§17.174 — research endpoints (autonomous research, URL/PDF/GitHub modes, sessions).

Extracted from ``app/main.py`` as part of the §17.174 router refactor.
Endpoint paths, function names, tags, and response_models are
preserved verbatim so the committed ``docs/openapi.json`` snapshot
stays byte-identical post-refactor.

Routes:
  POST   /research                              — research_endpoint (SSE)
  POST   /research/start                        — research_start_endpoint (§17.820 detached kickoff)
  POST   /research/reply                        — research_reply_endpoint (SSE)
  GET    /research/verify/{session_id}          — research_verify_endpoint
  POST   /research/pdf                          — research_pdf_endpoint (SSE)
  GET    /research/pdf                          — research_pdf_upload_page (template)
  GET    /research/sessions                     — list_research_sessions
  GET    /research/sessions/{session_id}        — get_research_session (§17.820 detail)
  DELETE /research/sessions/{session_id}        — delete_research_session
  PATCH  /research/sessions/{session_id}        — rename_research_session
"""
from typing import Annotated
import logging
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.authz import (
    Principal,
    assert_visible,
    get_principal,
    owner_filter,
)
from app.config import settings
from app.database import async_session, get_db
from app.modules.research_agent import run_research, run_research_pdf, resume_research
from app.schemas import (
    DeleteResponse,
    RESEARCH_SESSION_STATUSES,
    ResearchInput,
    ResearchReplyInput,
    ResearchSessionListResponse,
    ResearchSessionRenameInput,
    ResearchSessionSummary,
)
from app.utils.model_validation import _require_valid_models
from app.utils.sse import _sse_with_disconnect_watch
from app.utils.upload import read_capped

router = APIRouter()

logger = logging.getLogger("scaffold")

# Template registry — kept local because the only template the router
# uses is research_pdf_upload.html. Moving to a shared location is
# fine but doesn't help anyone today.
templates = Jinja2Templates(directory="app/templates")
# §17.460 — expose the per-request CSP nonce to templates so inline
# <script>/<style> in research_pdf_upload.html can be nonce'd under the
# strict (no 'unsafe-inline') CSP.
from app.middleware.security_headers import current_csp_nonce as _current_csp_nonce
from app.utils.ids import UuidPath
templates.env.globals["csp_nonce"] = _current_csp_nonce


@router.post("/research", tags=["Research"])
async def research_endpoint(
    body: ResearchInput,
    request: Request,
    principal: Principal = Depends(get_principal),
):
    """Autonomous research: decompose topic → search → extract → ingest → iterate.

    Wrapped in ``_sse_with_disconnect_watch`` so that client disconnect
    propagates a ``CancelledError`` into the research generator within ~1s,
    allowing the lifecycle wrapper to finalize the session as ``cancelled``.
    """
    await _require_valid_models(body.model_overrides)
    # §17.810 — stamp the creating principal as the session owner.
    source = run_research(
        topic=body.topic,
        depth=body.depth,
        domain=body.domain,
        model_overrides=body.model_overrides,
        owner=principal.identity,
    )
    return StreamingResponse(
        _sse_with_disconnect_watch(request, source),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@router.post("/research/start", tags=["Research"])
async def research_start_endpoint(
    body: ResearchInput,
    principal: Principal = Depends(get_principal),
):
    """§17.820 (plan 5.9) — fire-and-forget research kickoff.

    The detached-run capability the /web form had (spawn_research_background,
    §17.454 pattern: the 20-60 min run survives the browser closing), now as
    JSON. The streaming ``POST /research`` cancels the session when the client
    disconnects — this one finalizes server-side regardless. Progress lands in
    ``GET /research/sessions`` / ``GET /research/sessions/{id}``.
    """
    await _require_valid_models(body.model_overrides)
    from app.modules.research_agent import spawn_research_background

    spawn_research_background(
        body.topic.strip(),
        depth=body.depth,
        domain=body.domain,
        model_overrides=body.model_overrides,
        owner=principal.identity,
    )
    logger.info(
        "research_start_detached topic=%s depth=%s owner=%s",
        body.topic.strip()[:80], body.depth, principal.identity,
    )
    return {"status": "started", "topic": body.topic.strip(), "depth": body.depth}


# ── §17.1120 — detached research runs (the §17.1007 shape) ───────────────────

@router.post("/research/runs", tags=["Research"])
async def research_run_start(
    body: ResearchInput,
    principal: Principal = Depends(get_principal),
):
    """Start research as a DETACHED run and return its ``run_id`` at once. The
    run keeps going when the client goes away; ``GET /research/runs/{id}/stream``
    tails it (backlog first), ``POST /research/runs/{id}/cancel`` stops it.
    Replaces the streaming ``POST /research`` for the SPA (which cancelled the
    session on disconnect) and the handle-less ``POST /research/start``."""
    await _require_valid_models(body.model_overrides)
    from app.modules import research_runs
    run_id = research_runs.start(
        lambda: run_research(
            topic=body.topic, depth=body.depth, domain=body.domain,
            model_overrides=body.model_overrides, owner=principal.identity,
        ),
        owner=principal.identity, kind="research", label=body.topic.strip(),
    )
    return {"run_id": run_id, "status": "started", "topic": body.topic.strip(), "depth": body.depth}


@router.post("/research/runs/reply", tags=["Research"])
async def research_run_reply(
    body: ResearchReplyInput,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Answer an ``awaiting_reply`` session as a detached run (same contract as
    ``POST /research/runs``). The session must be visible to the caller."""
    await _require_valid_models(body.model_overrides)
    owner_clause, owner_params = owner_filter(principal, column="owner")
    row = (await db.execute(text(
        f"SELECT id FROM research_sessions WHERE id = :id{owner_clause}"
    ), {"id": body.session_id, **owner_params})).first()
    if not row:
        raise HTTPException(status_code=404, detail=f"research_session not found: {body.session_id}")
    from app.modules import research_runs
    run_id = research_runs.start(
        lambda: resume_research(body.session_id, body.reply, model_overrides=body.model_overrides),
        owner=principal.identity, kind="research_reply", label=body.session_id,
    )
    return {"run_id": run_id, "status": "started", "session_id": body.session_id}


@router.get("/research/runs/{run_id}", tags=["Research"])
async def research_run_status(
    run_id: UuidPath,
    principal: Principal = Depends(get_principal),
):
    """Is this run in flight? (The SPA asks before re-attaching after a reload
    or a dropped stream.)"""
    from app.modules import research_runs
    try:
        UUID(run_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="run_id must be a valid UUID")
    if not research_runs.visible(run_id, principal):
        raise HTTPException(status_code=404, detail=f"research run not found: {run_id}")
    m = research_runs.meta(run_id) or {}
    return {"run_id": run_id, "running": research_runs.is_running(run_id),
            "kind": m.get("kind"), "label": m.get("label")}


@router.get("/research/runs/{run_id}/stream", tags=["Research"])
async def research_run_stream(
    run_id: UuidPath,
    principal: Principal = Depends(get_principal),
):
    """Tail a research run: its backlog first, then live frames until it ends.
    After the run has ended, the frames persisted to Redis are replayed once
    (so a reload shows what happened). 404 when there is neither."""
    from app.modules import research_runs
    try:
        UUID(run_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="run_id must be a valid UUID")
    if not research_runs.visible(run_id, principal):
        raise HTTPException(status_code=404, detail=f"research run not found: {run_id}")
    run = research_runs.get_run(run_id)
    if run is not None:
        from app.modules.run_broker import subscribe
        source = subscribe(run)
    else:
        frames = await research_runs.replay(run_id)
        if not frames:
            raise HTTPException(status_code=404, detail=f"research run not found: {run_id}")

        async def _replay():
            for f in frames:
                yield f
        source = _replay()
    return StreamingResponse(source, media_type="text/event-stream",
                             headers={"X-Accel-Buffering": "no"})


@router.post("/research/runs/{run_id}/cancel", tags=["Research"])
async def research_run_cancel(
    run_id: UuidPath,
    principal: Principal = Depends(get_principal),
):
    """Stop a detached research run on purpose (closing the tab no longer does).
    The lifecycle wrapper finalizes the session as ``cancelled``."""
    from app.modules import research_runs
    try:
        UUID(run_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=422, detail="run_id must be a valid UUID")
    if not research_runs.visible(run_id, principal):
        raise HTTPException(status_code=404, detail=f"research run not found: {run_id}")
    return {"run_id": run_id, "cancelled": await research_runs.cancel(run_id)}


@router.get("/research/sessions/{session_id}", tags=["Management"])
async def get_research_session(
    session_id: UuidPath,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """§17.820 (plan 5.9) — single-session detail read.

    The /web detail page's 17-column payload (stats + summary + error), which
    the JSON surface lacked — ``GET /research/sessions`` is a thin list and
    ``/research/verify/{id}`` is a different (grounding) payload.
    """
    try:
        UUID(session_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="session_id must be a valid UUID")

    # §17.810 — owner predicate: a non-owner reads zero rows → 404.
    owner_clause, owner_params = owner_filter(principal, column="owner")
    row = (await db.execute(text(f"""
        SELECT id, topic, depth, domain, status, summary, error_message,
               iterations_completed, total_entries_extracted, total_entries_ingested,
               total_entries_rejected, total_urls_searched, total_queries,
               coverage_pct, duration_ms, created_at, completed_at
        FROM research_sessions WHERE id = :id{owner_clause}
    """), {"id": session_id, **owner_params})).mappings().first()
    if not row:
        raise HTTPException(status_code=404, detail=f"research_session not found: {session_id}")
    return dict(row)


@router.post("/research/reply", tags=["Research"])
async def research_reply_endpoint(
    body: ResearchReplyInput,
    request: Request,
    principal: Principal = Depends(get_principal),
):
    """Resume a paused research session with the user's clarification reply."""
    await _require_valid_models(body.model_overrides)
    # §17.810 — only the session owner (or admin) may resume it. Own a
    # short-lived session for the check so no connection is held across the SSE.
    async with async_session() as _s:
        await assert_visible(
            _s, principal, body.session_id,
            table="research_sessions",
            detail=f"research_session not found: {body.session_id}",
        )
    source = resume_research(
        session_id=body.session_id,
        user_reply=body.reply,
        model_overrides=body.model_overrides,
    )
    return StreamingResponse(
        _sse_with_disconnect_watch(request, source),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@router.get("/research/verify/{session_id}", tags=["Research"])
async def research_verify_endpoint(
    session_id: UuidPath,
    recheck: bool = Query(False, description="If true, HEAD-request each entry's source_url to surface upstream reachability state."),
    compare_hash: bool = Query(False, description="If true (§17.126), GET each URL and SHA256-compare against the stored raw_upstream_hash. Implies recheck=true."),
    principal: Principal = Depends(get_principal),
):
    """Session-scoped provenance audit (§17.114 + §17.121).

    Lists every Milvus entry produced by the given research session and
    reports its current state — present, superseded, or missing. Used to
    surface drift between what was ingested and what's currently in the
    index, without re-fetching upstream content. See
    ``app/modules/research_verify.py`` for the returned-shape contract.

    ``?recheck=true`` (§17.121) additionally HEAD-requests each entry's
    ``source_url`` and reports ``upstream_state`` (reachable / missing /
    forbidden / error / skipped) per entry plus rollup totals. Bounded
    concurrency (5). SSRF re-checked on every URL.

    Pre-§17.114 sessions have no provenance rows linked by session_id
    and return an empty ``entries`` list — that's expected, not an error.
    """
    from app.modules.research_verify import verify_session

    try:
        UUID(session_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail=f"Invalid session_id (must be UUID): {session_id!r}")

    async with async_session() as db_session:
        # §17.810 — ownership gate before disclosing another user's provenance.
        await assert_visible(
            db_session, principal, session_id,
            table="research_sessions",
            detail=f"research_session not found: {session_id}",
        )
        return await verify_session(
            db_session, session_id,
            recheck_upstream=recheck,
            compare_hash=compare_hash,
        )


@router.post("/research/pdf", tags=["Research"])
async def research_pdf_endpoint(
    request: Request,
    file: UploadFile = File(...),
    extractor: str = Query("auto", pattern="^(auto|pypdf|plumber)$"),
    domain: str | None = Query(None),
    principal: Principal = Depends(get_principal),
):
    """PDF ingestion: upload PDF → extract → ingest → stream SSE."""
    # UploadFile.filename is str | None per Starlette; multipart uploads
    # without a filename header would crash on .lower() — guard explicitly.
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="File must be a PDF")

    # §17.180: Content-Length pre-check (cheap rejection before we touch the body
    # at all). The header is advisory — multipart/chunked uploads may omit or
    # spoof it — but for well-formed clients this short-circuits oversize uploads
    # without any I/O. The streaming read below is the authoritative cap.
    cl_header = request.headers.get("content-length")
    if cl_header and cl_header.isdigit():
        if int(cl_header) > settings.research_max_pdf_bytes:
            cap_mb = settings.research_max_pdf_bytes // (1024 * 1024)
            raise HTTPException(
                status_code=413,
                detail=(
                    f"PDF exceeds {cap_mb}MB cap "
                    f"(declared Content-Length {cl_header} bytes)"
                ),
            )

    # §17.180: stream-read in 1 MiB chunks and abort mid-stream once we've read
    # past the cap. Pre-§17.180 used ``await file.read()`` which buffered the
    # entire payload before any size check — a hostile uploader could inflate
    # orchestrator RSS by the full ``research_max_pdf_bytes`` before being
    # rejected. Now the peak is cap + one chunk regardless of actual upload size.
    pdf_bytes = await read_capped(
        file, settings.research_max_pdf_bytes, label="PDF",
    )
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Empty file")

    await _require_valid_models(None)

    source = run_research_pdf(
        pdf_bytes=pdf_bytes,
        filename=file.filename,
        extractor=extractor,
        domain=domain,
        model_overrides=None,
        owner=principal.identity,
    )
    return StreamingResponse(
        _sse_with_disconnect_watch(request, source),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@router.get("/research/pdf", tags=["Research"])
async def research_pdf_upload_page(request: Request):
    """Drag-and-drop HTML upload page for PDF ingestion."""
    return templates.TemplateResponse(request, "research_pdf_upload.html")


@router.get("/research/sessions", response_model=ResearchSessionListResponse, tags=["Management"])
async def list_research_sessions(
    status: str | None = None,
    q: str | None = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 25,
    offset: Annotated[int, Query(ge=0, le=1_000_000)] = 0,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Paginated research session list with optional status + topic search."""
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be 1..100")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")

    where_clauses = []
    params: dict = {}
    if status:
        if status not in RESEARCH_SESSION_STATUSES:
            raise HTTPException(status_code=422, detail=f"invalid status: {status}")
        where_clauses.append("status = :status")
        params["status"] = status
    if q:
        where_clauses.append("topic ILIKE :q")
        params["q"] = f"%{q.strip()}%"
    # §17.810 — non-admin sees only their own sessions; admin sees all.
    owner_clause, owner_params = owner_filter(principal, column="owner")
    if owner_clause:
        where_clauses.append(owner_clause.removeprefix(" AND "))
        params.update(owner_params)
    # SAFE: where_clauses contain only bind-parameter placeholders (:status, :q,
    # :principal_owner); all user values flow through `params` dict. Do not
    # interpolate user input into where_clauses directly without enum/whitelist
    # validation first.
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    total_row = await db.execute(text(f"SELECT COUNT(*) FROM research_sessions {where_sql}"), params)
    total = total_row.scalar() or 0

    params["limit"] = limit
    params["offset"] = offset
    rows = await db.execute(text(f"""
        SELECT id, topic, status, depth, domain, iterations_completed,
               total_entries_ingested, coverage_pct, created_at, updated_at
        FROM research_sessions
        {where_sql}
        ORDER BY updated_at DESC
        LIMIT :limit OFFSET :offset
    """), params)
    sessions = [
        ResearchSessionSummary(
            id=str(r.id),
            topic=r.topic,
            status=r.status,
            depth=r.depth,
            domain=r.domain,
            iterations_completed=r.iterations_completed,
            total_entries_ingested=r.total_entries_ingested,
            coverage_pct=r.coverage_pct,
            created_at=r.created_at.isoformat(),
            updated_at=r.updated_at.isoformat(),
        )
        for r in rows.fetchall()
    ]
    return ResearchSessionListResponse(sessions=sessions, total=total, limit=limit, offset=offset)


@router.delete("/research/sessions/{session_id}", response_model=DeleteResponse, tags=["Management"])
async def delete_research_session(
    session_id: UuidPath,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Hard-delete a research session. Note: KB entries already in Milvus are NOT
    removed; this only drops the session metadata + state snapshot."""
    try:
        UUID(session_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="session_id must be a valid UUID")

    # §17.810 — owner predicate: a non-owner deletes zero rows → 404.
    owner_clause, owner_params = owner_filter(principal, column="owner")
    r = await db.execute(
        text(f"DELETE FROM research_sessions WHERE id = :id{owner_clause} RETURNING id"),
        {"id": session_id, **owner_params},
    )
    if r.fetchone() is None:
        raise HTTPException(status_code=404, detail=f"research_session not found: {session_id}")
    await db.commit()
    return DeleteResponse(deleted=True, id=session_id)


@router.patch("/research/sessions/{session_id}", response_model=ResearchSessionSummary, tags=["Management"])
async def rename_research_session(
    session_id: UuidPath,
    body: ResearchSessionRenameInput,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Rename a research session (set topic)."""
    try:
        UUID(session_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="session_id must be a valid UUID")

    # §17.810 — owner predicate: a non-owner updates zero rows → 404.
    owner_clause, owner_params = owner_filter(principal, column="owner")
    r = await db.execute(text(f"""
        UPDATE research_sessions SET topic = :topic, updated_at = NOW()
        WHERE id = :id{owner_clause}
        RETURNING id, topic, status, depth, domain, iterations_completed,
                  total_entries_ingested, coverage_pct, created_at, updated_at
    """), {"id": session_id, "topic": body.topic, **owner_params})
    row = r.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"research_session not found: {session_id}")
    await db.commit()
    return ResearchSessionSummary(
        id=str(row.id), topic=row.topic, status=row.status,
        depth=row.depth, domain=row.domain,
        iterations_completed=row.iterations_completed,
        total_entries_ingested=row.total_entries_ingested,
        coverage_pct=row.coverage_pct,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )
