"""Scaffold Engine — FastAPI orchestrator."""

import asyncio
import logging
import os
import time
import uuid as _uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

import httpx
import structlog
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
from app.modules.dag_generator import generate_dag as _generate_dag
from app.modules.execution_agent import execute_next_node, skip_node, retry_failed_node, execute_all_nodes
from app.modules.execution_handler import execution_status
from app.modules.gt_browser import gt_list, gt_search, gt_detail, gt_stats
from app.modules.gt_extractor import extract_ground_truths
from app.modules.idea_refinement import refine_idea
from app.modules.ideation_workflow import analyze_and_confirm, research_and_compile
from app.modules.research_agent import run_research, run_research_pdf, resume_research
from app.modules.prompt_inspector import list_prompts, get_prompt, update_prompt
from app.modules.prompt_optimizer import optimize_prompt
from app.modules.rag_pipeline import query_rag as _query_rag
from app.routers.status import router as status_router
from app.schemas import (
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
)

logger = logging.getLogger("scaffold")
templates = Jinja2Templates(directory="app/templates")

setup_logging(
    json_logs=os.getenv("LOG_JSON_FORMAT", "true").lower() == "true",
    log_level=os.getenv("LOG_LEVEL", settings.log_level),
    log_file=os.getenv("LOG_FILE"),
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

    # Verify Milvus
    try:
        milvus_connections.connect(alias="default", uri=settings.milvus_uri)
        logger.info("milvus_connected: uri=%s", settings.milvus_uri)
    except Exception as e:
        logger.warning("milvus_connection_failed: uri=%s error=%s", settings.milvus_uri, e)

    # Database connectivity is verified by first request via get_db()
    logger.info("engine_started: log_level=%s", settings.log_level)

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
    milvus_connections.disconnect("default")
    logger.info("engine_stopped")


app = FastAPI(
    dependencies=[Depends(require_api_key)],
    title="Scaffold Engine",
    description="Self-hosted RAG-powered workflow orchestrator",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(ErrorLoggingMiddleware)
app.add_middleware(PerformanceMiddleware)
app.include_router(status_router)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    structlog.contextvars.clear_contextvars()
    request_id = request.headers.get("X-Request-ID", str(_uuid.uuid4()))
    structlog.contextvars.bind_contextvars(request_id=request_id)
    start = time.perf_counter_ns()
    response = await call_next(request)
    duration_ms = (time.perf_counter_ns() - start) / 1_000_000
    structlog.stdlib.get_logger("api.access").info(
        "http_request",
        http_method=request.method,
        http_path=request.url.path,
        http_status=response.status_code,
        duration_ms=round(duration_ms, 2),
    )
    response.headers["X-Request-ID"] = request_id
    return response


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
            async with httpx.AsyncClient(timeout=5) as client:
                resp = await client.get(f"{settings.ollama_base_url}/api/tags")
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

    pg, ollama, milvus = await asyncio.gather(
        _check_pg(), _check_ollama(), _check_milvus(),
        return_exceptions=True,
    )
    if isinstance(pg, Exception):
        pg = {"status": "down", "latency_ms": 0}
    if isinstance(ollama, Exception):
        ollama = {"status": "down", "latency_ms": 0, "models_loaded": []}
    if isinstance(milvus, Exception):
        milvus = {"status": "down", "latency_ms": 0, "collection_count": 0, "entry_count": 0}

    # Redis + cache stats (reuse async connection from embedding cache)
    cache_stats: dict = {}
    try:
        from app.utils.embedding_cache import get_cache
        _cache = get_cache()
        cache_stats = _cache.stats
        _redis_conn = await _cache._get_redis()
        await asyncio.wait_for(_redis_conn.ping(), timeout=2.0)
        _key_count = await asyncio.wait_for(_redis_conn.dbsize(), timeout=2.0)
        redis_info = {"status": "up", "keys": _key_count}
    except Exception:
        redis_info = {"status": "down", "keys": 0}
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
    """Raise 422 if any Ollama-routed models are missing."""
    missing = await validate_models(overrides)
    if missing:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "model_validation_failed",
                "missing_models": missing,
                "hint": "Check Ollama with: curl http://localhost:11434/api/tags",
            },
        )

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
    """Step 13: Query RAG pipeline (embed → search → rerank → return)."""
    return await _query_rag(
        body.query,
        top_k=body.top_k,
        confidence_threshold=body.confidence_threshold,
        skip_rerank=body.skip_rerank,
        include_history=body.include_history,
        domain=body.domain,
    )
@app.get("/rag/dedup")
async def list_dedup_log(limit: int = 50, offset: int = 0):
    """List logged near-duplicate rejections for manual review."""
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
async def gt_list_endpoint(page: int = 1, per_page: int = 20):
    """Step 19: Paginated list of all TOON entries."""
    try:
        return await gt_list(page=page, per_page=per_page)
    except Exception as e:
        logger.error("/gt/list failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/gt/search")
async def gt_search_endpoint(body: GtSearchInput):
    """Step 19: Semantic search TOON entries."""
    try:
        return await gt_search(query=body.query, top_k=body.top_k, domain=body.domain)
    except Exception as e:
        logger.error("/gt/search failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/gt/detail/{entry_id}")
async def gt_detail_endpoint(entry_id: str):
    """Step 19: Full content of a specific TOON entry."""
    try:
        return await gt_detail(entry_id=entry_id)
    except Exception as e:
        logger.error("/gt/detail failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/gt/stats")
async def gt_stats_endpoint():
    """Step 19: Collection summary."""
    try:
        return await gt_stats()
    except Exception as e:
        logger.error("/gt/stats failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))


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
    )
    return PromptOptimizeResult(**result.__dict__)


@app.post("/execute", response_model=ExecutionResult, tags=["Step 15"])
async def execute_next(body: ExecuteNextInput, db: AsyncSession = Depends(get_db)):
    """Step 15: Execute the next pending DAG node for a job."""
    return await execute_next_node(
        job_id=body.job_id,
        db=db,
        skip_optimize=body.skip_optimize,
        skip_verify=body.skip_verify,
        model_override=body.model_override,
    )


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
async def research_endpoint(body: ResearchInput):
    """Autonomous research: decompose topic → search → extract → ingest → iterate."""
    await _require_valid_models(body.model_overrides)
    return StreamingResponse(
        run_research(
            topic=body.topic,
            depth=body.depth,
            domain=body.domain,
            model_overrides=body.model_overrides,
        ),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@app.post("/research/reply", tags=["Research"])
async def research_reply_endpoint(body: ResearchReplyInput):
    """Resume a paused research session with the user's clarification reply."""
    await _require_valid_models(body.model_overrides)
    return StreamingResponse(
        resume_research(
            session_id=body.session_id,
            user_reply=body.reply,
            model_overrides=body.model_overrides,
        ),
        media_type="text/event-stream",
    )


@app.post("/research/pdf", tags=["Research"])
async def research_pdf_endpoint(
    file: UploadFile = File(...),
    extractor: str = Query("auto", regex="^(auto|pypdf|plumber)$"),
    domain: str | None = Query(None),
):
    """PDF ingestion: upload PDF → extract → ingest → stream SSE."""
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="File must be a PDF")

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=400, detail="Empty file")
    if len(pdf_bytes) > 20 * 1024 * 1024:
        raise HTTPException(status_code=413, detail=f"PDF exceeds 20MB cap ({len(pdf_bytes)} bytes)")

    await _require_valid_models(None)

    return StreamingResponse(
        run_research_pdf(
            pdf_bytes=pdf_bytes,
            filename=file.filename,
            extractor=extractor,
            domain=domain,
            model_overrides=None,
        ),
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

    if body.depth not in ("shallow", "medium", "deep"):
        raise HTTPException(status_code=422, detail="depth must be shallow|medium|deep")

    await _require_valid_models(body.model_overrides)

    result = await db.execute(text("""
        INSERT INTO scheduled_jobs (topic, depth, cron_expression, timezone, enabled)
        VALUES (:topic, :depth, :cron, :tz, TRUE)
        RETURNING id, topic, depth, cron_expression, timezone, enabled,
                  last_run_at, last_status, last_job_id, next_run_at,
                  run_count, failure_count, created_at
    """), {"topic": body.topic, "depth": body.depth, "cron": body.cron_expression, "tz": body.timezone})
    row = result.mappings().first()
    await db.commit()

    await add_schedule(row["id"], row["topic"], row["depth"], row["cron_expression"], row["timezone"])
    return dict(row)


@app.get("/schedule")
async def list_schedules(db: AsyncSession = Depends(get_db)):
    rows = (await db.execute(text("""
        SELECT id, topic, depth, cron_expression, timezone, enabled,
               last_run_at, last_status, last_job_id, next_run_at,
               run_count, failure_count, created_at
        FROM scheduled_jobs ORDER BY created_at DESC
    """))).mappings().all()
    return {"schedules": [dict(r) for r in rows]}


@app.delete("/schedule/{schedule_id}")
async def delete_schedule(schedule_id: int, db: AsyncSession = Depends(get_db)):
    from app.scheduler import remove_schedule
    result = await db.execute(text("DELETE FROM scheduled_jobs WHERE id = :id RETURNING id"),
                              {"id": schedule_id})
    if not result.mappings().first():
        raise HTTPException(status_code=404, detail="schedule not found")
    await db.commit()
    await remove_schedule(schedule_id)
    return {"deleted": schedule_id}


