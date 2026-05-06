"""Scaffold Engine — FastAPI orchestrator."""

import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import FastAPI, Request, HTTPException, Depends, UploadFile, File, Query
from pymilvus import connections as milvus_connections, utility, Collection
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from app.model_router import close_client, validate_models
from starlette.responses import StreamingResponse
from fastapi.templating import Jinja2Templates

from app.auth import require_api_key
from app.config import settings
from app.modules.cleanup import start_cleanup_task, reap_stale_jobs
from app.database import get_db, engine, async_session
from app.logging_config import setup_logging
from app.middleware.error_logging import ErrorLoggingMiddleware
from app.middleware.performance import PerformanceMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.modules.dag_generator import generate_dag as _generate_dag
from app.modules.execution_agent import execute_next_node, skip_node, retry_failed_node, execute_all_nodes
from app.modules.execution_handler import execution_status
from app.modules.gt_browser import gt_list, gt_search, gt_detail, gt_stats
from app.modules.gt_extractor import extract_ground_truths
from app.modules.idea_refinement import refine_idea
from app.modules.ideation_workflow import analyze_and_confirm, research_and_compile
from app.modules.research_agent import run_research, run_research_pdf, resume_research
from app.modules.prompt_inspector import list_prompts, get_prompt, update_prompt, get_history
from app.modules.prompt_optimizer import optimize_prompt
from app.modules.rag_pipeline import query_rag as _query_rag
from app.routers.assist import router as assist_router
from app.routers.status import router as status_router
from app.schemas import (
    JOB_STATUSES,
    RESEARCH_SESSION_STATUSES,
    ConfirmInput,
    DagInput,
    ExecRetryInput,
    ExecuteNextInput,
    ExecutionResult,
    GtInput,
    GtSearchInput,
    IdeaInput,
    PromptOptimizeInput,
    PromptOptimizeResult,
    PromptUpdateInput,
    RagInput,
    ResearchInput,
    ResearchReplyInput,
    ScheduleCreate,
    ScheduleResponse,
    SkipNodeInput,
    JobRenameInput,
    JobSummary,
    JobListResponse,
    ResearchSessionRenameInput,
    ResearchSessionSummary,
    ResearchSessionListResponse,
    DeleteResponse,
)

logger = logging.getLogger("scaffold")
templates = Jinja2Templates(directory="app/templates")

setup_logging(
    json_logs=settings.log_json_format,
    log_level=settings.log_level,
    log_file=settings.log_file,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: verify Ollama, Milvus, PostgreSQL connectivity."""

    # Verify Ollama
    try:
        from app.model_router import _get_client
        resp = await _get_client().get(f"{settings.ollama_base_url}/api/tags")
        models = [m["name"] for m in resp.json().get("models", [])]
        logger.info("ollama_connected: models_available=%d", len(models))
    except Exception as e:
        logger.warning("ollama_connection_failed: url=%s error=%s", settings.ollama_base_url, e)

    # Verify Milvus — PyMilvus is sync; wrap so the event loop is not
    # blocked during the (potentially slow) initial connect handshake.
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None,
            lambda: milvus_connections.connect(alias="default", uri=settings.milvus_uri),
        )
        logger.info("milvus_connected: uri=%s", settings.milvus_uri)
    except Exception as e:
        logger.warning("milvus_connection_failed: uri=%s error=%s", settings.milvus_uri, e)

    # Database connectivity is verified by first request via get_db()
    # Run schema migrations before anything else touches the DB (#10).
    # Opt out with SCAFFOLD_RUN_MIGRATIONS_ON_STARTUP=false (default: true).
    _run_migs = os.getenv("SCAFFOLD_RUN_MIGRATIONS_ON_STARTUP", "true").strip().lower()
    if _run_migs not in ("0", "false", "no", "off"):
        try:
            from app.migrations import run_migrations
            mig_result = await run_migrations()
            if mig_result.get("status") == "error":
                logger.error("migrations_failed_at_startup: %s", mig_result)
            elif mig_result.get("applied"):
                logger.info(
                    "migrations_applied_at_startup: count=%d files=%s",
                    len(mig_result["applied"]), mig_result["applied"],
                )
        except Exception as exc:
            logger.error("migrations_hook_crashed: error=%s", exc)
    else:
        logger.info("migrations_skipped_by_env: SCAFFOLD_RUN_MIGRATIONS_ON_STARTUP=%s", _run_migs)

    logger.info("engine_started: log_level=%s", settings.log_level)
    # Eager-init shared HTTP clients (searxng, github, generic) — no lazy path
    from app.utils.http_clients import init_clients
    init_clients()

    # Pre-warm reranker (Apr 26 2026): avoid ~13s cold-load on first user request.
    # Opt out: SCAFFOLD_PREWARM_RERANKER=false
    if os.getenv("SCAFFOLD_PREWARM_RERANKER", "true").strip().lower() not in ("0", "false", "no", "off"):
        try:
            import asyncio
            from app.rerankers import _get_cross_encoder
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, _get_cross_encoder)
            logger.info("reranker_prewarmed")
        except Exception as exc:
            logger.warning("reranker_prewarm_failed: %s", exc)

    # Optional startup cleanup
    if os.getenv("CLEANUP_ON_STARTUP", "").lower() == "true":
        logger.info('event="startup_cleanup_begin"')
        try:
            async with async_session() as db:
                result = await reap_stale_jobs(db)
                logger.info(
                    'event="startup_cleanup_complete" running_to_failed=%s planning_to_cancelled=%s',
                    result["running_to_failed"], result["planning_to_cancelled"],
                )
        except Exception as exc:
            logger.error('event="startup_cleanup_failed" error=%s', exc)

    _cleanup_task = start_cleanup_task()
    # Start APScheduler (rehydrates scheduled_jobs from DB)
    try:
        from app.scheduler import init_scheduler
        await init_scheduler()
    except Exception as exc:
        logger.error('event="scheduler_init_failed" error=%s', exc)
    yield

    # Shutdown
    _cleanup_task.cancel()
    try:
        await _cleanup_task
    except asyncio.CancelledError:
        pass
    try:
        from app.scheduler import shutdown_scheduler
        await shutdown_scheduler()
    except Exception as exc:
        logger.warning('event="scheduler_shutdown_failed" error=%s', exc)
    await close_client()
    from app.utils.http_clients import close_clients
    await close_clients()
    # PyMilvus disconnect is sync; wrap on the same async-first principle
    # as the startup connect above.
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, lambda: milvus_connections.disconnect("default"),
        )
    except Exception as exc:
        logger.warning('event="milvus_disconnect_failed" error=%s', exc)
    try:
        await engine.dispose()
    except Exception as exc:
        logger.warning('event="engine_dispose_failed" error=%s', exc)
    logger.info("engine_stopped")


app = FastAPI(
    dependencies=[Depends(require_api_key)],
    title="Scaffold Engine",
    description="Self-hosted RAG-powered workflow orchestrator",
    version="0.1.0",
    lifespan=lifespan,
)

# Middleware executes in reverse registration order: incoming request
# flows RequestId (outermost) -> Performance -> ErrorLogging (innermost) ->
# endpoint. HTTPException is intercepted by Starlette's own ExceptionMiddleware
# before it can reach our ErrorLoggingMiddleware.dispatch — so 4xx paths
# return through the perf middleware normally and ErrorLogging only ever
# sees genuine 5xx exceptions.
app.add_middleware(ErrorLoggingMiddleware)
app.add_middleware(PerformanceMiddleware)
app.add_middleware(RequestIdMiddleware)
app.include_router(status_router)
app.include_router(assist_router)


# Note: request-id binding + X-Request-ID header are handled by RequestIdMiddleware
# (app/middleware/request_id.py); per-request access logging by PerformanceMiddleware
# (app/middleware/performance.py). A previous duplicate function-based middleware
# here generated a second UUID and emitted an access log without the request_id
# contextvar bound — removed.


# ── Health check (no auth — exempt from global require_api_key) ──────

@app.get("/health", dependencies=[])
async def health():
    """Concurrent dependency health check — no auth required."""

    async def _check_pg():
        t0 = time.monotonic()
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return {"status": "up", "latency_ms": round((time.monotonic() - t0) * 1000)}
        except Exception:
            return {"status": "down", "latency_ms": round((time.monotonic() - t0) * 1000)}

    async def _check_ollama():
        t0 = time.monotonic()
        try:
            from app.utils.http_clients import get_ollama_client
            client = get_ollama_client()
            resp = await client.get(f"{settings.ollama_base_url}/api/tags", timeout=5.0)
            resp.raise_for_status()
            models = [m["name"] for m in resp.json().get("models", [])]
            return {"status": "up", "latency_ms": round((time.monotonic() - t0) * 1000), "models_loaded": models}
        except Exception:
            return {"status": "down", "latency_ms": round((time.monotonic() - t0) * 1000), "models_loaded": []}

    async def _check_milvus():
        t0 = time.monotonic()
        try:
            loop = asyncio.get_running_loop()
            def _sync():
                colls = utility.list_collections()
                entry_count = 0
                if "toon_v2" in colls:
                    col = Collection("toon_v2")
                    entry_count = col.num_entities
                return len(colls), entry_count
            coll_count, entries = await asyncio.wait_for(
                loop.run_in_executor(None, _sync), timeout=5.0
            )
            return {
                "status": "up",
                "latency_ms": round((time.monotonic() - t0) * 1000),
                "collection_count": coll_count,
                "entry_count": entries,
            }
        except Exception:
            return {
                "status": "down",
                "latency_ms": round((time.monotonic() - t0) * 1000),
                "collection_count": 0,
                "entry_count": 0,
            }

    async def _check_redis():
        cache_stats: dict = {}
        try:
            from app.utils.embedding_cache import get_cache
            cache = get_cache()
            cache_stats = cache.stats
            redis_conn = await cache._get_redis()
            await asyncio.wait_for(redis_conn.ping(), timeout=2.0)
            key_count = await asyncio.wait_for(redis_conn.dbsize(), timeout=2.0)
            return {"status": "up", "keys": key_count}, cache_stats
        except Exception:
            return {"status": "down", "keys": 0}, cache_stats

    # Each _check_* wraps its body in try/except Exception and returns a
    # dict on failure, so gather() cannot surface Exception objects from
    # these tasks; ``return_exceptions=True`` is left in only as
    # belt-and-suspenders for BaseException-derived cases (which we'd
    # actually want to propagate, not absorb).
    pg, ollama, milvus, redis_pair = await asyncio.gather(
        _check_pg(), _check_ollama(), _check_milvus(), _check_redis(),
        return_exceptions=True,
    )
    redis_info, cache_stats = redis_pair
    checks = {"postgresql": pg, "ollama": ollama, "milvus": milvus, "redis": redis_info, "embedding_cache": cache_stats}
    pg_up = pg["status"] == "up"
    ollama_up = ollama["status"] == "up"
    milvus_up = milvus["status"] == "up"

    if pg_up and ollama_up and milvus_up:
        status = "healthy"
    elif pg_up and ollama_up:
        status = "degraded"
    else:
        status = "unhealthy"

    return {
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }


# ── Stale job cleanup (uses global auth) ─────────────────────────────

@app.post("/jobs/cleanup", tags=["ops"])
async def cleanup_stale_jobs(db: AsyncSession = Depends(get_db)):
    """Find and resolve stale/orphaned jobs. Requires API key (global auth)."""
    result = await reap_stale_jobs(db)
    return {
        "cleaned": result,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }







async def _require_valid_models(overrides: dict | None = None):
    """Raise 503 if Ollama unreachable, 422 if models missing."""
    missing = await validate_models(overrides)
    if missing is None:
        raise HTTPException(
            status_code=503,
            detail={
                "error": "ollama_unreachable",
                "hint": "Check Ollama with: curl http://localhost:11434/api/tags",
            },
        )
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "model_validation_failed",
                "missing_models": missing,
                "hint": "Check Ollama with: curl http://localhost:11434/api/tags",
            },
        )


async def _sse_with_disconnect_watch(request: Request, source):
    """Interleave SSE keepalive comments to force Starlette to notice
    client disconnect quickly.

    Starlette's ``listen_for_disconnect`` only raises when uvicorn's ASGI
    ``receive`` delivers an ``http.disconnect`` message, which in turn
    only happens when the server-side socket is actively probed. During
    long generator awaits (LLM calls, HTTP fetches), no probe occurs, so
    a ``kill -9`` on the client can go undetected for 30+ minutes.

    Fix: emit an SSE comment line (``: keepalive\n\n``) every
    ``KEEPALIVE_INTERVAL`` seconds when the underlying generator is idle.
    Each comment write exercises the socket; a write to a dead socket
    raises ``ConnectionError`` which Starlette surfaces as a cancellation
    into the generator. The lifecycle wrapper in ``research_agent``
    catches the ``CancelledError`` in its ``finally`` block and finalizes
    the session as ``cancelled`` with ``error_message='client_disconnect'``.
    """
    KEEPALIVE_INTERVAL = 2.0  # seconds
    gen = source.__aiter__()
    next_task: asyncio.Task | None = None

    try:
        while True:
            if next_task is None:
                next_task = asyncio.create_task(gen.__anext__())

            done, _pending = await asyncio.wait(
                {next_task}, timeout=KEEPALIVE_INTERVAL,
            )
            if not done:
                # Generator is still computing — emit a socket-probing comment.
                # If the client is gone, this write fails and Starlette cancels us.
                yield ": keepalive\n\n"
                continue

            try:
                chunk = next_task.result()
            except StopAsyncIteration:
                return
            finally:
                next_task = None

            yield chunk
    finally:
        if next_task is not None and not next_task.done():
            next_task.cancel()
            try:
                await next_task
            except BaseException:
                # Best-effort cleanup: swallow CancelledError + any
                # exception the inner generator surfaces during shutdown
                # so we don't mask the outer flow's exit reason.
                pass
        aclose = getattr(gen, "aclose", None)
        if aclose is not None:
            try:
                await aclose()
            except Exception:
                pass


@app.post("/ideas")
async def submit_idea(body: IdeaInput, db=Depends(get_db)):
    """Step 10: Submit new idea → trigger refinement."""
    await _require_valid_models(body.model_overrides)
    result = await refine_idea(body.idea, db, model=body.model, domain=body.domain, model_overrides=body.model_overrides)
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(
            status_code=result.get("http_status", 500),
            detail=result["error"],
        )
    return result

@app.post("/ideate")
async def ideate_endpoint(body: IdeaInput, db=Depends(get_db)):
    """Phase 1: Analyze idea, assess feasibility, halt for confirmation."""
    await _require_valid_models(body.model_overrides)
    result = await analyze_and_confirm(body.idea, db, model=body.model, domain=body.domain, model_overrides=body.model_overrides)
    if isinstance(result, dict) and "error" in result:
        raise HTTPException(
            status_code=result.get("http_status", 500),
            detail=result["error"],
        )
    return result

@app.post("/ideate/confirm")
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

@app.get("/dag/{job_id}")
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


@app.post("/dag")
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


@app.post("/rag")
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
    )
    if result.get("status") == "error":
        raise HTTPException(
            status_code=503,
            detail=result.get("error", "RAG pipeline error"),
        )
    return result
@app.get("/rag/dedup")
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

@app.post("/gt")
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


@app.get("/gt/list")
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

@app.post("/gt/search")
async def gt_search_endpoint(body: GtSearchInput):
    """Step 19: Semantic search TOON entries."""
    return await gt_search(query=body.query, top_k=body.top_k, domain=body.domain, include_history=body.include_history)

@app.get("/gt/detail/{entry_id}")
async def gt_detail_endpoint(entry_id: str):
    """Step 19: Full content of a specific TOON entry."""
    return await gt_detail(entry_id=entry_id)

@app.get("/gt/stats")
async def gt_stats_endpoint():
    """Step 19: Collection summary."""
    return await gt_stats()


@app.get("/prompts/{job_id}")
async def prompts_list(job_id: str, db: AsyncSession = Depends(get_db)):
    """List all prompts for a job's DAG nodes."""
    try:
        result = await list_prompts(UUID(job_id), db)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")


@app.get("/prompts/{job_id}/{node_key}")
async def prompts_detail(job_id: str, node_key: str, db: AsyncSession = Depends(get_db)):
    """Get full prompt for a specific node."""
    try:
        result = await get_prompt(UUID(job_id), node_key, db)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")


@app.get("/prompts/{job_id}/{node_key}/history")
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


@app.post("/prompts/{job_id}/{node_key}")
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


@app.get("/exec/status/{job_id}")
async def exec_status(job_id: str, db: AsyncSession = Depends(get_db)):
    """Get execution state for a job."""
    try:
        result = await execution_status(UUID(job_id), db)
        if "error" in result:
            raise HTTPException(status_code=404, detail=result["error"])
        return result
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")


@app.post("/exec/retry")
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




@app.post("/optimize", response_model=PromptOptimizeResult, tags=["Step 14"])
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


@app.post("/execute", response_model=ExecutionResult, tags=["Step 15"])
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


@app.post("/execute/all", tags=["Step 15"])
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

@app.post("/research", tags=["Research"])
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


@app.post("/research/reply", tags=["Research"])
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


@app.post("/research/pdf", tags=["Research"])
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

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(pdf_bytes) > settings.research_max_pdf_bytes:
        cap_mb = settings.research_max_pdf_bytes // (1024 * 1024)
        raise HTTPException(
            status_code=413,
            detail=f"PDF exceeds {cap_mb}MB cap ({len(pdf_bytes)} bytes)",
        )

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


@app.get("/research/pdf", tags=["Research"])
async def research_pdf_upload_page(request: Request):
    """Drag-and-drop HTML upload page for PDF ingestion."""
    return templates.TemplateResponse(request, "research_pdf_upload.html")



@app.get("/research/history", tags=["Research"])
async def research_history():
    """List recent research sessions (last 50)."""
    async with async_session() as db:
        rows = await db.execute(
            text("""
                SELECT id, topic, depth, domain, iterations_completed,
                       total_entries_extracted, total_entries_ingested,
                       total_entries_rejected, total_urls_searched,
                       total_queries, duration_ms, coverage_pct,
                       status, created_at, completed_at
                FROM research_sessions
                ORDER BY created_at DESC
                LIMIT 50
            """)
        )
        sessions = [dict(r) for r in rows.mappings().all()]
        for s in sessions:
            s["id"] = str(s["id"])
        return {"sessions": sessions, "count": len(sessions)}


@app.get("/research/history/{session_id}", tags=["Research"])
async def research_history_detail(session_id: str):
    """Get a single research session by ID. Returns 404 if not found."""
    try:
        parsed = UUID(session_id)
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid session ID format")
    async with async_session() as db:
        row = await db.execute(
            text("SELECT * FROM research_sessions WHERE id = :sid"),
            {"sid": str(parsed)},
        )
        session = row.mappings().first()
        if not session:
            raise HTTPException(status_code=404, detail=f"Research session {session_id} not found")
        result = dict(session)
        result["id"] = str(result["id"])
        return result
@app.post("/skip", response_model=ExecutionResult, tags=["Step 15"])
async def skip_node_endpoint(body: SkipNodeInput, db: AsyncSession = Depends(get_db)):
    """Step 15: Skip a specific DAG node."""
    return await skip_node(job_id=body.job_id, node_key=body.node_key, db=db)


# ---------------- Scheduled research jobs ----------------

@app.post("/schedule", response_model=ScheduleResponse)
async def create_schedule(body: ScheduleCreate, db: AsyncSession = Depends(get_db)):
    """Create a recurring research schedule."""
    from apscheduler.triggers.cron import CronTrigger
    from app.scheduler import add_schedule

    try:
        CronTrigger.from_crontab(body.cron_expression, timezone=body.timezone)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid cron expression or timezone: {exc}")

    await _require_valid_models(body.model_overrides)

    result = await db.execute(text("""
        INSERT INTO scheduled_jobs (topic, depth, cron_expression, timezone, enabled)
        VALUES (:topic, :depth, :cron, :tz, TRUE)
        RETURNING id, topic, depth, cron_expression, timezone, enabled,
                  last_run_at, last_status, last_job_id, next_run_at,
                  run_count, failure_count, created_at
    """), {"topic": body.topic, "depth": body.depth, "cron": body.cron_expression, "tz": body.timezone})
    row = result.mappings().first()

    # APScheduler registration + next_run_at UPDATE both run in this same
    # session so the UPDATE can see the still-uncommitted INSERT. On any
    # failure, db.rollback() unwinds the INSERT and add_schedule() has
    # already removed its APScheduler entry, leaving system state aligned.
    try:
        next_run = await add_schedule(
            db, row["id"], row["topic"], row["depth"],
            row["cron_expression"], row["timezone"],
        )
        await db.commit()
    except Exception as exc:
        await db.rollback()
        raise HTTPException(status_code=502, detail=f"scheduler registration failed: {exc}")
    response = dict(row)
    response["next_run_at"] = next_run
    return response


@app.get("/schedule")
async def list_schedules(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    if limit < 1 or limit > 200:
        raise HTTPException(status_code=422, detail="limit must be 1..200")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")
    total = (await db.execute(
        text("SELECT COUNT(*) FROM scheduled_jobs")
    )).scalar() or 0
    rows = (await db.execute(text("""
        SELECT id, topic, depth, cron_expression, timezone, enabled,
               last_run_at, last_status, last_job_id, next_run_at,
               run_count, failure_count, created_at
        FROM scheduled_jobs
        ORDER BY created_at DESC
        LIMIT :limit OFFSET :offset
    """), {"limit": limit, "offset": offset})).mappings().all()
    return {
        "schedules": [dict(r) for r in rows],
        "total": total,
        "limit": limit,
        "offset": offset,
    }


@app.delete("/schedule/{schedule_id}")
async def delete_schedule(schedule_id: int, db: AsyncSession = Depends(get_db)):
    from app.scheduler import delete_schedule as _scheduler_delete

    deleted = await _scheduler_delete(db, schedule_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="schedule not found")
    await db.commit()
    return {"deleted": schedule_id}




# phase_c_management_endpoints --------------------------------------------------
# Job + research-session management endpoints (Phase C)
# ------------------------------------------------------------------------------

@app.get("/jobs", response_model=JobListResponse, tags=["Management"])
async def list_jobs(
    status: str | None = None,
    q: str | None = None,
    limit: int = 25,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Paginated job list with optional status filter and title search."""
    if limit < 1 or limit > 100:
        raise HTTPException(status_code=422, detail="limit must be 1..100")
    if offset < 0:
        raise HTTPException(status_code=422, detail="offset must be >= 0")

    where_clauses = []
    params: dict = {}
    if status:
        if status not in JOB_STATUSES:
            raise HTTPException(status_code=422, detail=f"invalid status: {status}")
        where_clauses.append("j.status = :status")
        params["status"] = status
    if q:
        where_clauses.append("j.title ILIKE :q")
        params["q"] = f"%{q.strip()}%"

    # SAFE: where_clauses contain only bind-parameter placeholders (:status, :q);
    # all user values flow through `params` dict. Do not interpolate user input
    # into where_clauses directly without enum/whitelist validation first.
    where_sql = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    total_row = await db.execute(text(f"SELECT COUNT(*) FROM jobs j {where_sql}"), params)
    total = total_row.scalar() or 0

    params["limit"] = limit
    params["offset"] = offset
    rows = await db.execute(text(f"""
        SELECT j.id, j.title, j.status, j.created_at, j.updated_at,
               COALESCE(n.cnt, 0) AS node_count
        FROM jobs j
        LEFT JOIN (SELECT job_id, COUNT(*) AS cnt FROM dag_nodes GROUP BY job_id) n
          ON n.job_id = j.id
        {where_sql}
        ORDER BY j.updated_at DESC
        LIMIT :limit OFFSET :offset
    """), params)
    jobs = [
        JobSummary(
            id=str(r.id),
            title=r.title or "",
            status=r.status,
            node_count=r.node_count,
            created_at=r.created_at.isoformat(),
            updated_at=r.updated_at.isoformat(),
        )
        for r in rows.fetchall()
    ]
    return JobListResponse(jobs=jobs, total=total, limit=limit, offset=offset)


@app.delete("/jobs/{job_id}", response_model=DeleteResponse, tags=["Management"])
async def delete_job(job_id: str, db: AsyncSession = Depends(get_db)):
    """Hard-delete a job. Cascade removes dag_nodes / execution_logs / artifacts /
    error_logs (FK ON DELETE CASCADE). Sets performance_logs.job_id NULL."""
    try:
        UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="job_id must be a valid UUID")

    r = await db.execute(text("DELETE FROM jobs WHERE id = :id RETURNING id"), {"id": job_id})
    if r.fetchone() is None:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    await db.commit()
    return DeleteResponse(deleted=True, id=job_id)


@app.patch("/jobs/{job_id}", response_model=JobSummary, tags=["Management"])
async def rename_job(job_id: str, body: JobRenameInput, db: AsyncSession = Depends(get_db)):
    """Rename a job (set title)."""
    try:
        UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="job_id must be a valid UUID")

    r = await db.execute(text("""
        UPDATE jobs SET title = :title, updated_at = NOW()
        WHERE id = :id
        RETURNING id, title, status, created_at, updated_at,
                  (SELECT COUNT(*) FROM dag_nodes WHERE job_id = :id) AS node_count
    """), {"id": job_id, "title": body.title})
    row = r.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    await db.commit()
    return JobSummary(
        id=str(row.id), title=row.title, status=row.status,
        node_count=row.node_count or 0,
        created_at=row.created_at.isoformat(),
        updated_at=row.updated_at.isoformat(),
    )


@app.get("/research/sessions", response_model=ResearchSessionListResponse, tags=["Management"])
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


@app.delete("/research/sessions/{session_id}", response_model=DeleteResponse, tags=["Management"])
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


@app.patch("/research/sessions/{session_id}", response_model=ResearchSessionSummary, tags=["Management"])
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

# end phase_c_management_endpoints ---------------------------------------------
