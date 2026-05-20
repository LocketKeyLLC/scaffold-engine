"""§17.174 — ground truth (TOON KB) endpoints.

Extracted from ``app/main.py`` as part of the §17.174 router refactor.
Endpoint paths, function names, tags, and response_models are
preserved verbatim so the committed ``docs/openapi.json`` snapshot
stays byte-identical post-refactor.

Routes:
  POST /gt                      — extract_gt (Step 12)
  GET  /gt/list                 — gt_list_endpoint (Step 19)
  POST /gt/search               — gt_search_endpoint (Step 19)
  GET  /gt/detail/{entry_id}    — gt_detail_endpoint (Step 19)
  GET  /gt/stats                — gt_stats_endpoint (Step 19)
"""
from fastapi import APIRouter, HTTPException

from app.modules.gt_browser import gt_list, gt_search, gt_detail, gt_stats
from app.modules.gt_extractor import extract_ground_truths
from app.schemas import GtInput, GtSearchInput
from app.utils.model_validation import _require_valid_models

router = APIRouter()


@router.post("/gt")
async def extract_gt(body: GtInput):
    """Step 12: Extract ground truths via SearXNG + LLM distillation."""
    await _require_valid_models({"model_general": body.model} if body.model else None)
    return await extract_ground_truths(
        body.topic,
        queries=body.queries,
        push_to_github=body.push_to_github,
        target_file=body.target_file,
        model=body.model,
    )


@router.get("/gt/list")
async def gt_list_endpoint(
    page: int = 1,
    per_page: int = 20,
    include_history: bool = False,
    domain: str | None = None,
):
    """Step 19: Paginated list of all TOON entries."""
    if page < 1:
        raise HTTPException(status_code=422, detail="page must be >= 1")
    if per_page < 1 or per_page > 100:
        raise HTTPException(status_code=422, detail="per_page must be 1..100")
    if domain is not None:
        from app.config import VALID_DOMAINS
        if domain not in VALID_DOMAINS:
            raise HTTPException(
                status_code=422,
                detail=f"domain must be one of {sorted(VALID_DOMAINS)}",
            )
    return await gt_list(
        page=page,
        per_page=per_page,
        include_history=include_history,
        domain=domain,
    )


@router.post("/gt/search")
async def gt_search_endpoint(body: GtSearchInput):
    """Step 19: Semantic search TOON entries."""
    return await gt_search(query=body.query, top_k=body.top_k, domain=body.domain, include_history=body.include_history)


@router.get("/gt/detail/{entry_id}")
async def gt_detail_endpoint(entry_id: str):
    """Step 19: Full content of a specific TOON entry."""
    return await gt_detail(entry_id=entry_id)


@router.get("/gt/stats")
async def gt_stats_endpoint():
    """Step 19: Collection summary."""
    return await gt_stats()
