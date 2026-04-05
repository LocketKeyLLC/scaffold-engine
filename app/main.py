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
from fastapi import FastAPI, Request, HTTPException, Depends
from pymilvus import connections as milvus_connections, utility, Collection
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.auth import require_api_key
from app.config import settings
from app.database import get_db, engine
from app.logging_config import setup_logging
from app.middleware.error_logging import ErrorLoggingMiddleware
from app.middleware.performance import PerformanceMiddleware
from app.modules.dag_generator import generate_dag as _generate_dag
from app.modules.execution_agent import execute_next_node, skip_node, retry_failed_node, execute_all_nodes
from app.modules.execution_handler import execution_status, retry_node
from app.modules.gt_browser import gt_list, gt_search, gt_detail, gt_stats
from app.modules.gt_extractor import extract_ground_truths
from app.modules.idea_refinement import refine_idea
from app.modules.prompt_inspector import list_prompts, get_prompt, update_prompt
from app.modules.prompt_optimizer import optimize_prompt
from app.modules.rag_pipeline import query_rag as _query_rag
from app.routers.status import router as status_router
from app.schemas import (
    ExecuteNextInput,
    ExecutionResult,
    PromptOptimizeInput,
    PromptOptimizeResult,
    SkipNodeInput,
)

logger = logging.getLogger("scaffold")

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
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{settings.ollama_base_url}/api/tags")
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
            async for db in get_db():
                r1 = await db.execute(text("""
                    UPDATE jobs SET status='failed',
                        compiled_output='Job timed out after 30 minutes of inactivity',
                        updated_at=NOW()
                    WHERE status='running' AND updated_at < NOW() - INTERVAL '30 minutes'
                """))
                r2 = await db.execute(text("""
                    UPDATE jobs SET status='cancelled', updated_at=NOW()
                    WHERE status='planning' AND updated_at < NOW() - INTERVAL '60 minutes'
                """))
                await db.commit()
                logger.info(
                    'event="startup_cleanup_complete" running_to_failed=%s planning_to_cancelled=%s',
                    r1.rowcount, r2.rowcount,
                )
                break
        except Exception as exc:
            logger.error('event="startup_cleanup_failed" error=%s', exc)

    yield

    # Shutdown
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
                if "technical_knowledge" in colls:
                    col = Collection("technical_knowledge")
                    col.flush()
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

    checks = {"postgresql": pg, "ollama": ollama, "milvus": milvus}
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
    now = datetime.now(timezone.utc)

    # Running > 30 min → failed
    stale_running = await db.execute(
        text("""
            SELECT id, updated_at FROM jobs
            WHERE status = 'running'
              AND updated_at < NOW() - INTERVAL '30 minutes'
        """)
    )
    running_rows = stale_running.fetchall()
    for row in running_rows:
        job_id, updated_at = row[0], row[1]
        age_min = round((now - updated_at.replace(tzinfo=timezone.utc)).total_seconds() / 60, 1)
        await db.execute(
            text("UPDATE jobs SET status='failed', compiled_output=:msg, updated_at=NOW() WHERE id=:jid"),
            {"jid": str(job_id), "msg": "Job timed out after 30 minutes of inactivity"},
        )
        logger.info(
            'event="stale_job_cleaned" job_id=%s old_status=running new_status=failed age_minutes=%s',
            job_id, age_min,
        )

    # Planning > 60 min → cancelled
    stale_planning = await db.execute(
        text("""
            SELECT id, updated_at FROM jobs
            WHERE status = 'planning'
              AND updated_at < NOW() - INTERVAL '60 minutes'
        """)
    )
    planning_rows = stale_planning.fetchall()
    for row in planning_rows:
        job_id, updated_at = row[0], row[1]
        age_min = round((now - updated_at.replace(tzinfo=timezone.utc)).total_seconds() / 60, 1)
        await db.execute(
            text("UPDATE jobs SET status='cancelled', updated_at=NOW() WHERE id=:jid"),
            {"jid": str(job_id)},
        )
        logger.info(
            'event="stale_job_cleaned" job_id=%s old_status=planning new_status=cancelled age_minutes=%s',
            job_id, age_min,
        )

    await db.commit()

    return {
        "cleaned": {
            "running_to_failed": len(running_rows),
            "planning_to_cancelled": len(planning_rows),
        },
        "timestamp": now.isoformat(),
    }


# === Endpoint stubs — each will be implemented as a separate module ===


class IdeaInput(BaseModel):
    idea: str
    domain: str | None = None
    model: str | None = None

@app.post("/ideas")
async def submit_idea(body: IdeaInput, db=Depends(get_db)):
    """Step 10: Submint new idea → trigger refinement."""
    return await refine_idea(body.idea, db, model=body.model, domain=body.domain)

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


class DagInput(BaseModel):
    job_id: str
    model: str | None = None

@app.post("/dag")
async def generate_dag_endpoint(body: DagInput, db=Depends(get_db)):
    """Step 11: Generate DAG from refined idea brief."""
    return await _generate_dag(body.job_id, db, model=body.model)


class RagInput(BaseModel):
    query: str
    top_k: int = 10
    confidence_threshold: float = 0.8
    skip_rerank: bool = False

@app.post("/rag")
async def query_rag(body: RagInput):
    """Step 13: Query RAG pipeline (embed → search → rerank → return)."""
    return await _query_rag(
        body.query,
        top_k=body.top_k,
        confidence_threshold=body.confidence_threshold,
        skip_rerank=body.skip_rerank,
    )


class GtInput(BaseModel):
    topic: str
    queries: list[str] | None = None
    push_to_github: bool = False
    target_file: str | None = None
    model: str | None = None

@app.post("/gt")
async def extract_gt(body: GtInput):
    """Step 12: Extract ground truths via SearXNG + LLM distillation."""
    return await extract_ground_truths(
        body.topic,
        queries=body.queries,
        push_to_github=body.push_to_github,
        target_file=body.target_file,
        model=body.model,
    )


class GtSearchInput(BaseModel):
    query: str
    top_k: int = 10

@app.get("/gt/list")
async def gt_list_endpoint(page: int = 1, per_page: int = 20):
    """Step 19: Paginated list of all TOON entries."""
    try:
        return await gt_list(page=page, per_page=per_page)
    except Exception as e:
        logger.error("/gt/list failed: %s", e)
        return {"error": str(e)}

@app.post("/gt/search")
async def gt_search_endpoint(body: GtSearchInput):
    """Step 19: Semantic search TOON entries."""
    try:
        return await gt_search(query=body.query, top_k=body.top_k)
    except Exception as e:
        logger.error("/gt/search failed: %s", e)
        return {"error": str(e)}

@app.get("/gt/detail/{entry_id}")
async def gt_detail_endpoint(entry_id: str):
    """Step 19: Full content of a specific TOON entry."""
    try:
        return await gt_detail(entry_id=entry_id)
    except Exception as e:
        logger.error("/gt/detail failed: %s", e)
        return {"error": str(e)}

@app.get("/gt/stats")
async def gt_stats_endpoint():
    """Step 19: Collection summary."""
    try:
        return await gt_stats()
    except Exception as e:
        logger.error("/gt/stats failed: %s", e)
        return {"error": str(e)}


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
async def prompts_update(job_id: str, node_key: str, request: Request, db: AsyncSession = Depends(get_db)):
    """Update the optimized prompt for a pending/failed node."""
    try:
        body = await request.json()
        new_prompt = body.get("prompt", "").strip()
        if not new_prompt:
            raise HTTPException(status_code=400, detail="Missing 'prompt' in request body")
        result = await update_prompt(UUID(job_id), node_key, new_prompt, db)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")


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
async def exec_retry(request: Request, db: AsyncSession = Depends(get_db)):
    """Reset a failed node to pending for retry."""
    try:
        body = await request.json()
        job_id = body.get("job_id", "")
        node_key = body.get("node_key", "")
        if not job_id or not node_key:
            raise HTTPException(status_code=400, detail="Missing job_id or node_key")
        result = await retry_node(UUID(job_id), node_key, db)
        if "error" in result:
            raise HTTPException(status_code=400, detail=result["error"])
        return result
    except ValueError:
        raise HTTPException(status_code=400, detail="Invalid job_id format")




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
async def execute_all_endpoint(body: ExecuteNextInput, db: AsyncSession = Depends(get_db)):
    """Execute all DAG nodes in sequence, streaming SSE events.

    Auto-generates DAG if none exists.  Failed nodes are skipped;
    downstream nodes blocked by failures are reported at the end.
    """
    return StreamingResponse(
        execute_all_nodes(body.job_id, db),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},  # disable nginx buffering
    )


@app.post("/skip", response_model=ExecutionResult, tags=["Step 15"])
async def skip_node_endpoint(body: SkipNodeInput, db: AsyncSession = Depends(get_db)):
    """Step 15: Skip a specific DAG node."""
    return await skip_node(job_id=body.job_id, node_key=body.node_key, db=db)


@app.post("/retry", tags=["Step 15"])
async def retry_node_endpoint(body: SkipNodeInput, db: AsyncSession = Depends(get_db)):
    """Retry a failed DAG node — resets it and downstream nodes to pending."""
    return await retry_failed_node(job_id=body.job_id, node_key=body.node_key, db=db)