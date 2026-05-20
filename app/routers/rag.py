"""§17.174 — RAG query + dedup log endpoints.

Extracted from ``app/main.py`` as part of the §17.174 router refactor.
Endpoint paths, function names, tags, and response_models are
preserved verbatim so the committed ``docs/openapi.json`` snapshot
stays byte-identical post-refactor.

Routes:
  POST /rag         — query_rag (Step 13)
  GET  /rag/dedup   — list_dedup_log
"""
from fastapi import APIRouter, HTTPException
from sqlalchemy import text

from app.database import async_session
from app.modules.rag_pipeline import query_rag as _query_rag
from app.schemas import RagInput

router = APIRouter()


@router.post("/rag")
async def query_rag(body: RagInput):
    """Step 13: Query RAG pipeline (embed → search → rerank → return).

    #35: raises HTTPException on pipeline errors so clients get a proper 5xx
    instead of HTTP 200 with an error body. The underlying query_rag() still
    returns status="error" dicts so non-HTTP callers (execution_agent) can
    degrade gracefully.
    """
    result = await _query_rag(
        body.query,
        top_k=body.top_k,
        confidence_threshold=body.confidence_threshold,
        skip_rerank=body.skip_rerank,
        include_history=body.include_history,
        domain=body.domain,
        query_intent=body.query_intent,
    )
    if result.get("status") == "error":
        raise HTTPException(
            status_code=503,
            detail=result.get("error", "RAG pipeline error"),
        )
    return result


@router.get("/rag/dedup")
async def list_dedup_log(limit: int = 50, offset: int = 0):
    """List logged near-duplicate rejections for manual review."""
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be 1..200")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")
    async with async_session() as session:
        result = await session.execute(
            text(
                "SELECT id, new_content_hash, existing_entry_id, similarity_score, "
                "action_taken, created_at FROM dedup_log "
                "ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
            ),
            {"limit": limit, "offset": offset},
        )
        rows = result.mappings().all()

        count_result = await session.execute(text("SELECT COUNT(*) FROM dedup_log"))
        total = count_result.scalar()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "entries": [dict(r) for r in rows],
    }
