"""§17.174 — prompt inspection + editing endpoints.

Extracted from ``app/main.py`` as part of the §17.174 router refactor.
Endpoint paths, function names, tags, and response_models are
preserved verbatim so the committed ``docs/openapi.json`` snapshot
stays byte-identical post-refactor.

Routes:
  GET  /prompts/{job_id}                          — prompts_list
  GET  /prompts/{job_id}/{node_key}               — prompts_detail
  GET  /prompts/{job_id}/{node_key}/history       — prompts_history
  POST /prompts/{job_id}/{node_key}               — prompts_update
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.modules.prompt_inspector import list_prompts, get_prompt, update_prompt, get_history
from app.schemas import PromptUpdateInput

router = APIRouter()


@router.get("/prompts/{job_id}")
async def prompts_list(job_id: str, db: AsyncSession = Depends(get_db)):
    """List all prompts for a job's DAG nodes."""
    try:
        result = await list_prompts(UUID(job_id), db)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")


@router.get("/prompts/{job_id}/{node_key}")
async def prompts_detail(job_id: str, node_key: str, db: AsyncSession = Depends(get_db)):
    """Get full prompt for a specific node."""
    try:
        result = await get_prompt(UUID(job_id), node_key, db)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")


@router.get("/prompts/{job_id}/{node_key}/history")
async def prompts_history(job_id: str, node_key: str, db: AsyncSession = Depends(get_db)):
    """Return the audit trail of prompt edits for a node, newest-first.

    Closes audit items #7.8 (no audit trail) and #7.9 (structured response).
    """
    try:
        result = await get_history(UUID(job_id), node_key, db)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/prompts/{job_id}/{node_key}")
async def prompts_update(
    job_id: str,
    node_key: str,
    body: PromptUpdateInput,
    db: AsyncSession = Depends(get_db),
):
    """Update the optimized prompt for a pending/failed node."""
    new_prompt = body.prompt.strip()
    if not new_prompt:
        raise HTTPException(status_code=400, detail="Missing 'prompt' in request body")
    try:
        result = await update_prompt(UUID(job_id), node_key, new_prompt, db)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result
