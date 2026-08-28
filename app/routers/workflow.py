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
  GET  /exec/nodes/{job_id}  — exec_nodes (§17.471 — per-node output bodies)
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

from app.authz import (
    Principal,
    assert_visible,
    get_principal,
    owner_filter,
)
from app.config import settings
from app.database import async_session, get_db
from app.modules.dag_generator import generate_dag as _generate_dag
from app.modules.decomposition import (
    MIN_COMPONENTS,
    create_and_run_decomposition,
    extract_components,
)
from app.modules.execution_agent import (
    execute_next_node,
    skip_node,
    retry_failed_node,
    execute_all_nodes,
    _sse_event,  # §17.855 (F6) — shared SSE frame formatter for /jobs/{id}/advance
)
from app.modules.execution_handler import execution_status, node_outputs
from app.modules.idea_refinement import create_ideation_job, refine_idea
from app.modules.ideation_workflow import (
    analyze_and_confirm,
    get_ideation_slot_sem,
    research_and_compile,
    spawn_phase1_background,
)
from app.modules.profiles import (  # §17.809 — per-job --quick
    mark_job_quick,
    merge_quick_overrides,
    resolve_job_overrides,
)
from app.modules.prompt_optimizer import optimize_prompt
from app.schemas import (
    AdvanceInput,
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
async def submit_idea(
    body: IdeaInput,
    db=Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Step 10: Submit new idea → trigger refinement."""
    # §17.809 — --quick: fast model map + flag the job (see /ideate).
    overrides = (
        merge_quick_overrides(body.model_overrides) if body.quick
        else body.model_overrides
    )
    await _require_valid_models(overrides)
    # §17.442 — bound concurrent ideation requests (router-layer so the job
    # isn't even created until a slot frees). See ideation_global_concurrency.
    # §17.810 — stamp the creating principal as the job owner.
    async with get_ideation_slot_sem():
        result = await refine_idea(body.idea, db, model=body.model, domain=body.domain, model_overrides=overrides, owner=principal.identity)
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(
            status_code=result.get("http_status", 500),
            detail=result["error"],
        )
    if body.quick and isinstance(result, dict):
        await mark_job_quick(result.get("job_id"))
    return result


@router.post("/ideate")
async def ideate_endpoint(
    body: IdeaInput,
    db=Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Phase 1: Analyze idea, assess feasibility, halt for confirmation."""
    # §17.809 — --quick: run Phase 1 on the fast "quick" model map and flag the
    # job so the later phases (confirm/dag/execute) stay fast without the client
    # having to re-send the map on every turn.
    overrides = (
        merge_quick_overrides(body.model_overrides) if body.quick
        else body.model_overrides
    )
    await _require_valid_models(overrides)
    # §17.442 — bound concurrent ideation requests (see ideation_global_concurrency).
    async with get_ideation_slot_sem():
        result = await analyze_and_confirm(body.idea, db, model=body.model, domain=body.domain, model_overrides=overrides, owner=principal.identity)
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(
            status_code=result.get("http_status", 500),
            detail=result["error"],
        )
    if body.quick and isinstance(result, dict):
        await mark_job_quick(result.get("job_id"))
    return result


@router.post("/ideate/start")
async def ideate_start_endpoint(
    body: IdeaInput,
    db=Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """§17.454 — Async kickoff for Phase 1. Creates the job row and returns its
    ``job_id`` immediately, then runs the 100-547s refinement in a background task.

    Lets the native web UI redirect straight to the live job-detail page on submit
    instead of the old hunt-for-your-job-in-a-filtered-list flow. The chat pipeline
    keeps using the synchronous ``POST /ideate``, which returns the full refined
    brief + feasibility in one shot. The model-validation gate runs here so a bad
    override 422s before any row is created."""
    # §17.809 — --quick: fast model map + flag the job before the background
    # Phase 1 spawns, so every phase (including this async refine) runs fast.
    overrides = (
        merge_quick_overrides(body.model_overrides) if body.quick
        else body.model_overrides
    )
    await _require_valid_models(overrides)
    # §17.810 — stamp owner at row creation; the background refine reuses this
    # same row (job_id), so ownership is set before Phase 1 even starts.
    job_id = await create_ideation_job(body.idea, db, domain=body.domain, owner=principal.identity)
    if body.quick:
        await mark_job_quick(job_id)
    spawn_phase1_background(
        job_id, body.idea,
        model=body.model, domain=body.domain,
        model_overrides=overrides,
    )
    return {"job_id": job_id, "status": "refining"}


@router.post("/decompose")
async def decompose_endpoint(
    body: IdeaInput,
    db=Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """§17.526 — split a multi-part idea into an umbrella + component child jobs,
    each run autonomously through the normal pipeline (Phase 1 → grounded Phase 2
    → DAG → execute). Returns the umbrella id + child roll-up immediately.

    If the idea has fewer than ``MIN_COMPONENTS`` separable parts, returns
    ``{"decomposed": false, "components": [...]}`` and creates nothing — the
    caller (the /go auto-chain) then falls back to the single-job ``POST /ideate``.
    The model-validation gate runs before any LLM work or row creation."""
    # §17.531 — server-side kill switch. Operators can disable decomposition
    # regardless of the pipeline's decompose_on_go valve; the caller falls back
    # to the single-job path (no LLM work wasted).
    if not settings.decompose_enabled:
        return {"decomposed": False, "reason": "disabled"}
    await _require_valid_models(body.model_overrides)
    async with get_ideation_slot_sem():
        components = await extract_components(
            body.idea, model_overrides=body.model_overrides,
        )
    if len(components) < MIN_COMPONENTS:
        # §17.530 — distinguish "genuinely one focused build" from "the splitter
        # LLM failed" so the caller/logs aren't blind to an extraction error.
        reason = "single_focus" if components else "extraction_unavailable"
        return {"decomposed": False, "components": components, "reason": reason}
    # §17.615 (audit #36) — serialize the check-and-create so two concurrent
    # /decompose calls can't both observe inflight below the cap and both fan
    # out, overrunning decompose_max_inflight_components. This xact-level advisory
    # lock is held on `db` until create_and_run_decomposition commits (its inserts
    # share this session), covering the whole count→insert critical section, and
    # releases before the background component pipelines spawn.
    await db.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": 9717615})
    # §17.531 — global fan-out cap: bound the total number of autonomous
    # component pipelines in flight across ALL umbrellas (cost / DoS guard).
    inflight = (await db.execute(
        text("""
            SELECT count(*) FROM jobs
            WHERE job_type = 'component'
              AND status NOT IN ('completed', 'failed', 'cancelled', 'blocked')
        """),
    )).scalar_one()
    if inflight + len(components) > settings.decompose_max_inflight_components:
        raise HTTPException(
            status_code=429,
            detail=(
                f"decomposition capacity reached ({inflight} components in flight, "
                f"cap {settings.decompose_max_inflight_components}); try again later"
            ),
        )
    result = await create_and_run_decomposition(
        body.idea, db, components=components, model_overrides=body.model_overrides,
        owner=principal.identity,
    )
    result["decomposed"] = True
    return result


@router.post("/ideate/confirm")
async def ideate_confirm_endpoint(
    body: ConfirmInput,
    db=Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Phase 2: User confirms -> research -> ingest -> compile -> present workflow."""
    # §17.810 — only the job's owner (or an admin) may advance it to Phase 2.
    await assert_visible(db, principal, body.job_id, detail=f"job not found: {body.job_id}")
    # §17.809 — if the job was started with --quick, layer the fast model map.
    overrides = await resolve_job_overrides(body.job_id, body.model_overrides)
    await _require_valid_models(overrides)
    # §17.820 — whitespace-only feedback means "no feedback" (ported from the
    # /web form's normalization; a "  " string would otherwise be folded into
    # the brief as if the user said something).
    feedback = (body.feedback or "").strip() or None
    result = await research_and_compile(
        body.job_id, db,
        user_feedback=feedback,
        push_to_github=body.push_to_github,
        model_overrides=overrides,
    )
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(
            status_code=result.get("http_status", 500),
            detail=result["error"],
        )
    return result


@router.get("/dag/{job_id}")
async def get_dag(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Step 18: Retrieve DAG nodes + job status for a job."""
    try:
        UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid job_id format")
    # §17.810 — owner predicate folded into the status probe; a non-owner sees
    # the same "Job not found" as a missing job.
    owner_clause, owner_params = owner_filter(principal, column="owner")
    row = await db.execute(
        text(f"SELECT status FROM jobs WHERE id = :id{owner_clause}"),
        {"id": job_id, **owner_params},
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
async def generate_dag_endpoint(
    body: DagInput,
    db=Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Step 11: Generate DAG from refined idea brief."""
    # §17.810 — only the owner (or admin) may plan a job's DAG.
    await assert_visible(db, principal, body.job_id, detail=f"job not found: {body.job_id}")
    # §17.809 — --quick jobs plan on the fast model map too.
    overrides = await resolve_job_overrides(body.job_id, body.model_overrides)
    await _require_valid_models(overrides)
    result = await _generate_dag(body.job_id, db, model=body.model, model_overrides=overrides)
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(
            status_code=result.get("http_status", 500),
            detail=result["error"],
        )
    return result


@router.post("/jobs/{job_id}/advance", tags=["Workflow"])
async def advance_job_endpoint(
    job_id: str,
    body: AdvanceInput,
    principal: Principal = Depends(get_principal),
):
    """§17.855 (audit F6) — server-side auto-chain, streamed as one SSE response:
    Phase 2 (research + ingest + compile) → plan (generate DAG) → optionally
    execute. Gives curl / CLI / SDK the macro the OWUI pipeline composed
    CLIENT-side (`scaffold_router._handle_confirm`), so those surfaces no longer
    have to call /ideate/confirm, /dag, /execute/all by hand and can't skip a
    step. Ownership-gated up front; each phase runs in its OWN short-lived
    session so no request-pool connection is pinned across the minutes-long run.

    Events: ``advance_phase`` {phase: research|planning} before each phase,
    ``error`` {phase, message} on a phase failure, then either the full
    ``/execute/all`` stream (execute=true) or a terminal ``advance_complete``.
    """
    try:
        UUID(job_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid job_id format")
    async with async_session() as _s:
        await assert_visible(_s, principal, job_id, detail=f"job not found: {job_id}")
    overrides = await resolve_job_overrides(job_id, body.model_overrides)
    await _require_valid_models(overrides)
    feedback = (body.feedback or "").strip() or None

    async def _stream():
        # Phase 2 — research → ingest → compile.
        yield _sse_event("advance_phase", {"job_id": job_id, "phase": "research"})
        async with async_session() as db:
            res = await research_and_compile(
                job_id, db, user_feedback=feedback, model_overrides=overrides,
            )
        if isinstance(res, dict) and "error" in res:
            yield _sse_event("error", {
                "job_id": job_id, "phase": "research",
                "message": res["error"], "http_status": res.get("http_status", 500),
            })
            return
        # Plan — generate the DAG (no execution).
        yield _sse_event("advance_phase", {"job_id": job_id, "phase": "planning"})
        async with async_session() as db:
            dag = await _generate_dag(job_id, db, model_overrides=overrides)
        if isinstance(dag, dict) and "error" in dag:
            yield _sse_event("error", {
                "job_id": job_id, "phase": "planning",
                "message": dag["error"], "http_status": dag.get("http_status", 500),
            })
            return
        if body.execute:
            # execute_all_nodes yields fully-formed SSE frames itself.
            async for chunk in execute_all_nodes(job_id, model_overrides=overrides):
                yield chunk
        else:
            yield _sse_event("advance_complete", {
                "job_id": job_id, "status": "planned",
                "node_count": dag.get("node_count") if isinstance(dag, dict) else None,
            })

    return StreamingResponse(
        _stream(), media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@router.get("/exec/status/{job_id}")
async def exec_status(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Get execution state for a job."""
    try:
        parsed_id = UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")
    # §17.810 — ownership gate before revealing execution state.
    await assert_visible(db, principal, str(parsed_id), detail=f"job not found: {job_id}")
    result = await execution_status(parsed_id, db)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.get("/exec/nodes/{job_id}")
async def exec_nodes(
    job_id: str,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """§17.471 — per-node output text (T1..Tn) for a job.

    Distinct from ``/exec/status``, which is summary-only (counts + node
    statuses, no output bodies). Backs the scaffold_router
    ``/results <job_id> nodes`` view so operators can pull up every
    node's full work product, not just the compiled deliverable — which
    ``execution_compile`` Strategy 0 limits to the ``is_output_node`` DAG
    leaves, dropping every interior node from a multi-leaf job.
    """
    try:
        parsed_id = UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")
    # §17.810 — ownership gate before revealing per-node outputs.
    await assert_visible(db, principal, str(parsed_id), detail=f"job not found: {job_id}")
    result = await node_outputs(parsed_id, db)
    if "error" in result:
        raise HTTPException(status_code=404, detail=result["error"])
    return result


@router.post("/exec/retry")
async def exec_retry(
    body: ExecRetryInput,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Reset a failed node to pending for retry."""
    if not body.job_id or not body.node_key:
        raise HTTPException(status_code=400, detail="Missing job_id or node_key")
    try:
        parsed_id = UUID(body.job_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")
    # §17.810 — only the owner (or admin) may retry a node on the job.
    await assert_visible(db, principal, str(parsed_id), detail=f"job not found: {body.job_id}")
    result = await retry_failed_node(parsed_id, body.node_key, db)
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
async def execute_next(
    body: ExecuteNextInput,
    principal: Principal = Depends(get_principal),
):
    """Step 15: Execute the next pending DAG node for a job.

    No request-scoped DB dependency: execute_next_node manages its own
    short-lived sessions, and the §17.810 ownership check opens its own.
    """
    # §17.810 — ownership gate. Own a short-lived session for the check so no
    # request-scoped connection is held across node execution. A malformed id is
    # left to execute_next_node's own error handling (skip the check).
    try:
        parsed_id = UUID(body.job_id)
    except (ValueError, TypeError, AttributeError):
        parsed_id = None
    if parsed_id is not None:
        async with async_session() as _s:
            await assert_visible(_s, principal, str(parsed_id), detail=f"job not found: {body.job_id}")
    # §17.809 — --quick jobs execute on the fast model map.
    overrides = await resolve_job_overrides(body.job_id, body.model_overrides)
    await _require_valid_models(overrides)
    result = await execute_next_node(
        job_id=body.job_id,
        skip_optimize=body.skip_optimize,
        skip_verify=body.skip_verify,
        model_overrides=overrides,
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
async def execute_all_endpoint(
    body: ExecuteNextInput,
    principal: Principal = Depends(get_principal),
):
    """Execute all DAG nodes in sequence, streaming SSE events.
    Auto-generates DAG if none exists.  Failed nodes are skipped;
    downstream nodes blocked by failures are reported at the end.
    """
    # §17.470 — validate the id BEFORE streaming, mirroring exec_status above.
    # Without this, a non-UUID job_id reaches the first asyncpg query inside
    # execute_all_nodes and its raw DataError ("invalid input for query argument
    # $1: ...") leaks to the client as an execution_failed SSE event.
    try:
        UUID(body.job_id)
    except (ValueError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid job_id format")
    # §17.810 — ownership gate BEFORE we start streaming (own short-lived session
    # so no connection is held across the whole SSE run).
    async with async_session() as _s:
        await assert_visible(_s, principal, body.job_id, detail=f"job not found: {body.job_id}")
    # §17.809 — --quick jobs execute every node on the fast model map.
    overrides = await resolve_job_overrides(body.job_id, body.model_overrides)
    await _require_valid_models(overrides)
    return StreamingResponse(
        execute_all_nodes(body.job_id, model_overrides=overrides),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},  # disable nginx buffering
    )


@router.post("/skip", response_model=ExecutionResult, tags=["Step 15"])
async def skip_node_endpoint(
    body: SkipNodeInput,
    db: AsyncSession = Depends(get_db),
    principal: Principal = Depends(get_principal),
):
    """Step 15: Skip a specific DAG node."""
    # §17.810 — ownership gate. body.job_id is a UUID-validated field on the
    # schema, so it is safe to hand straight to assert_visible.
    await assert_visible(db, principal, body.job_id, detail=f"job not found: {body.job_id}")
    return await skip_node(job_id=body.job_id, node_key=body.node_key, db=db)
