"""§17.174 — workflow endpoints (Phase 1, Phase 2, DAG, Execute, Optimize, Skip, Exec).

Extracted from ``app/main.py`` as part of the §17.174 router refactor.
Endpoint paths, function names, tags, and response_models are
preserved verbatim so the committed ``docs/openapi.json`` snapshot
stays byte-identical post-refactor.

Routes:
  POST /ideas              — submit_idea (Step 10)
  POST /ideate             — ideate_endpoint (Phase 1)
  POST /ideate/confirm     — ideate_confirm_endpoint (Phase 2)
  GET  /dag/{job_id}       — get_dag (Step 18)
  POST /dag                — generate_dag_endpoint (Step 11)
  GET  /exec/status/{job_id} — exec_status
  POST /exec/retry         — exec_retry
  POST /optimize           — optimize_endpoint (Step 14)
  POST /execute            — execute_next (Step 15)
  POST /execute/all        — execute_all_endpoint (Step 15, SSE)
  POST /skip               — skip_node_endpoint (Step 15)
"""
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.database import get_db
from app.modules.dag_generator import generate_dag as _generate_dag
from app.modules.execution_agent import (
    execute_next_node,
    skip_node,
    retry_failed_node,
    execute_all_nodes,
)
from app.modules.execution_handler import execution_status
from app.modules.idea_refinement import create_ideation_job, refine_idea
from app.modules.ideation_workflow import (
    analyze_and_confirm,
    get_ideation_slot_sem,
    research_and_compile,
    spawn_phase1_background,
)
from app.modules.prompt_optimizer import optimize_prompt
from app.schemas import (
    ConfirmInput,
    DagInput,
    ExecRetryInput,
    ExecuteNextInput,
    ExecutionResult,
    IdeaInput,
    PromptOptimizeInput,
    PromptOptimizeResult,
    SkipNodeInput,
)
from app.utils.model_validation import _require_valid_models

router = APIRouter()


@router.post("/ideas")
async def submit_idea(body: IdeaInput, db=Depends(get_db)):
    """Step 10: Submit new idea → trigger refinement."""
    await _require_valid_models(body.model_overrides)
    # §17.442 — bound concurrent ideation requests (router-layer so the job
    # isn't even created until a slot frees). See ideation_global_concurrency.
    async with get_ideation_slot_sem():
        result = await refine_idea(body.idea, db, model=body.model, domain=body.domain, model_overrides=body.model_overrides)
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(
            status_code=result.get("http_status", 500),
            detail=result["error"],
        )
    return result


@router.post("/ideate")
async def ideate_endpoint(body: IdeaInput, db=Depends(get_db)):
    """Phase 1: Analyze idea, assess feasibility, halt for confirmation."""
    await _require_valid_models(body.model_overrides)
    # §17.442 — bound concurrent ideation requests (see ideation_global_concurrency).
    async with get_ideation_slot_sem():
        result = await analyze_and_confirm(body.idea, db, model=body.model, domain=body.domain, model_overrides=body.model_overrides)
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(
            status_code=result.get("http_status", 500),
            detail=result["error"],
        )
    return result


@router.post("/ideate/start")
async def ideate_start_endpoint(body: IdeaInput, db=Depends(get_db)):
    """§17.454 — Async kickoff for Phase 1. Creates the job row and returns its
    ``job_id`` immediately, then runs the 100-547s refinement in a background task.

    Lets the native web UI redirect straight to the live job-detail page on submit
    instead of the old hunt-for-your-job-in-a-filtered-list flow. The chat pipeline
    keeps using the synchronous ``POST /ideate``, which returns the full refined
    brief + feasibility in one shot. The model-validation gate runs here so a bad
    override 422s before any row is created."""
    await _require_valid_models(body.model_overrides)
    job_id = await create_ideation_job(body.idea, db, domain=body.domain)
    spawn_phase1_background(
        job_id, body.idea,
        model=body.model, domain=body.domain,
        model_overrides=body.model_overrides,
    )
    return {"job_id": job_id, "status": "refining"}


@router.post("/ideate/confirm")
async def ideate_confirm_endpoint(body: ConfirmInput, db=Depends(get_db)):
    """Phase 2: User confirms -> research -> ingest -> compile -> present workflow."""
    await _require_valid_models(body.model_overrides)
    result = await research_and_compile(
        body.job_id, db,
        user_feedback=body.feedback,
        push_to_github=body.push_to_github,
        model_overrides=body.model_overrides,
    )
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(
            status_code=result.get("http_status", 500),
            detail=result["error"],
        )
    return result


@router.get("/dag/{job_id}")
async def get_dag(job_id: str, db: AsyncSession = Depends(get_db)):
    """Step 18: Retrieve DAG nodes + job status for a job."""
    try:
        UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid job_id format")
    row = await db.execute(
        text("SELECT status FROM jobs WHERE id = :id"),
        {"id": job_id},
    )
    job = row.mappings().first()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    nodes = await db.execute(
        text("SELECT node_key, title, status, depends_on, execution_order FROM dag_nodes WHERE job_id = :id ORDER BY execution_order"),
        {"id": job_id},
    )
    return {
        "job_id": job_id,
        "job_status": job["status"],
        "nodes": [dict(r) for r in nodes.mappings()],
    }


@router.post("/dag")
async def generate_dag_endpoint(body: DagInput, db=Depends(get_db)):
    """Step 11: Generate DAG from refined idea brief."""
    await _require_valid_models(body.model_overrides)
    result = await _generate_dag(body.job_id, db, model=body.model, model_overrides=body.model_overrides)
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(
            status_code=result.get("http_status", 500),
            detail=result["error"],
        )
    return result


@router.get("/exec/status/{job_id}")
async def exec_status(job_id: str, db: AsyncSession = Depends(get_db)):
    """Get execution state for a job."""
    try:
        result = await execution_status(UUID(job_id), db)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")


@router.post("/exec/retry")
async def exec_retry(body: ExecRetryInput, db: AsyncSession = Depends(get_db)):
    """Reset a failed node to pending for retry."""
    if not body.job_id or not body.node_key:
        raise HTTPException(status_code=400, detail="Missing job_id or node_key")
    try:
        result = await retry_failed_node(UUID(body.job_id), body.node_key, db)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")
    if "error" in result:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


@router.post("/optimize", response_model=PromptOptimizeResult, tags=["Step 14"])
async def optimize_endpoint(body: PromptOptimizeInput):
    """Step 14: Optimize a prompt — strip filler, reduce tokens, verify intent, score clarity."""
    result = await optimize_prompt(
        prompt=body.prompt,
        model_optimizer=body.model_optimizer,
        model_verifier=body.model_verifier,
        skip_verify=body.skip_verify,
        model_overrides=body.model_overrides,
    )
    return PromptOptimizeResult(**result.__dict__)


@router.post("/execute", response_model=ExecutionResult, tags=["Step 15"])
async def execute_next(body: ExecuteNextInput):
    """Step 15: Execute the next pending DAG node for a job.

    No DB dependency: execute_next_node manages its own short-lived sessions.
    """
    await _require_valid_models(body.model_overrides)
    result = await execute_next_node(
        job_id=body.job_id,
        skip_optimize=body.skip_optimize,
        skip_verify=body.skip_verify,
        model_overrides=body.model_overrides,
    )
    # Parity with /ideas, /dag, /rag: convert dict-error responses to a real
    # HTTP error so clients can dispatch on status code instead of having to
    # inspect the body. ExecutionResult lets ``error`` flow through; callers
    # that want soft failure can read execution_status() instead.
    if isinstance(result, dict) and result.get("status") == "failed" and result.get("error"):
        raise HTTPException(
            status_code=result.get("http_status", 500),
            detail=result["error"],
        )
    return result


@router.post("/execute/all", tags=["Step 15"])
async def execute_all_endpoint(body: ExecuteNextInput):
    """Execute all DAG nodes in sequence, streaming SSE events.
    Auto-generates DAG if none exists.  Failed nodes are skipped;
    downstream nodes blocked by failures are reported at the end.
    """
    await _require_valid_models(body.model_overrides)
    return StreamingResponse(
        execute_all_nodes(body.job_id, model_overrides=body.model_overrides),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},  # disable nginx buffering
    )


@router.post("/skip", response_model=ExecutionResult, tags=["Step 15"])
async def skip_node_endpoint(body: SkipNodeInput, db: AsyncSession = Depends(get_db)):
    """Step 15: Skip a specific DAG node."""
    return await skip_node(job_id=body.job_id, node_key=body.node_key, db=db)
