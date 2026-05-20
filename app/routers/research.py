"""§17.174 — research endpoints (autonomous research, URL/PDF/GitHub modes, sessions).

Extracted from ``app/main.py`` as part of the §17.174 router refactor.
Endpoint paths, function names, tags, and response_models are
preserved verbatim so the committed ``docs/openapi.json`` snapshot
stays byte-identical post-refactor.

Routes:
  POST   /research                              — research_endpoint (SSE)
  POST   /research/reply                        — research_reply_endpoint (SSE)
  GET    /research/verify/{session_id}          — research_verify_endpoint
  POST   /research/pdf                          — research_pdf_endpoint (SSE)
  GET    /research/pdf                          — research_pdf_upload_page (template)
  GET    /research/sessions                     — list_research_sessions
  DELETE /research/sessions/{session_id}        — delete_research_session
  PATCH  /research/sessions/{session_id}        — rename_research_session
"""
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.templating import Jinja2Templates
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

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

# Template registry — kept local because the only template the router
# uses is research_pdf_upload.html. Moving to a shared location is
# fine but doesn't help anyone today.
templates = Jinja2Templates(directory="app/templates")


@router.post("/research", tags=["Research"])
async def research_endpoint(body: ResearchInput, request: Request):
    """Autonomous research: decompose topic → search → extract → ingest → iterate.

    Wrapped in ``_sse_with_disconnect_watch`` so that client disconnect
    propagates a ``CancelledError`` into the research generator within ~1s,
    allowing the lifecycle wrapper to finalize the session as ``cancelled``.
    """
    await _require_valid_models(body.model_overrides)
    source = run_research(
        topic=body.topic,
        depth=body.depth,
        domain=body.domain,
        model_overrides=body.model_overrides,
    )
    return StreamingResponse(
        _sse_with_disconnect_watch(request, source),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@router.post("/research/reply", tags=["Research"])
async def research_reply_endpoint(body: ResearchReplyInput, request: Request):
    """Resume a paused research session with the user's clarification reply."""
    await _require_valid_models(body.model_overrides)
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
    session_id: str,
    recheck: bool = Query(False, description="If true, HEAD-request each entry's source_url to surface upstream reachability state."),
    compare_hash: bool = Query(False, description="If true (§17.126), GET each URL and SHA256-compare against the stored raw_upstream_hash. Implies recheck=true."),
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
    limit: int = 25,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
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
    # SAFE: where_clauses contain only bind-parameter placeholders (:status, :q);
    # all user values flow through `params` dict. Do not interpolate user input
    # into where_clauses directly without enum/whitelist validation first.
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
async def delete_research_session(session_id: str, db: AsyncSession = Depends(get_db)):
    """Hard-delete a research session. Note: KB entries already in Milvus are NOT
    removed; this only drops the session metadata + state snapshot."""
    try:
        UUID(session_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="session_id must be a valid UUID")

    r = await db.execute(text("DELETE FROM research_sessions WHERE id = :id RETURNING id"),
                          {"id": session_id})
    if r.fetchone() is None:
        raise HTTPException(status_code=404, detail=f"research_session not found: {session_id}")
    await db.commit()
    return DeleteResponse(deleted=True, id=session_id)


@router.patch("/research/sessions/{session_id}", response_model=ResearchSessionSummary, tags=["Management"])
async def rename_research_session(session_id: str, body: ResearchSessionRenameInput, db: AsyncSession = Depends(get_db)):
    """Rename a research session (set topic)."""
    try:
        UUID(session_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="session_id must be a valid UUID")

    r = await db.execute(text("""
        UPDATE research_sessions SET topic = :topic, updated_at = NOW()
        WHERE id = :id
        RETURNING id, topic, status, depth, domain, iterations_completed,
                  total_entries_ingested, coverage_pct, created_at, updated_at
    """), {"id": session_id, "topic": body.topic})
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
