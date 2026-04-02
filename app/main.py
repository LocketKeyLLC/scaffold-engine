"""Scaffold Engine — FastAPI orchestrator."""

import logging
from contextlib import asynccontextmanager

import httpx
from starlette.responses import StreamingResponse
from fastapi import FastAPI, Request, HTTPException, Depends
from pymilvus import connections as milvus_connections
from uuid import UUID

from app.config import settings
from app.middleware.error_logging import ErrorLoggingMiddleware
from app.middleware.performance import PerformanceMiddleware
from app.modules.gt_extractor import extract_ground_truths
from app.modules.rag_pipeline import query_rag as _query_rag
from app.modules.gt_browser import gt_list, gt_search, gt_detail, gt_stats
from app.modules.prompt_optimizer import optimize_prompt
from app.schemas import PromptOptimizeInput, PromptOptimizeResult
from app.modules.prompt_inspector import list_prompts, get_prompt, update_prompt
from app.modules.execution_handler import execution_status, retry_node
from app.auth import require_api_key

logger = logging.getLogger("scaffold")

import os
import time
import uuid as _uuid
import structlog
from app.logging_config import setup_logging

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
            logger.info("Ollama connected: %d models available", len(models))
    except Exception as e:
        logger.warning("Ollama not reachable at %s: %s", settings.ollama_base_url, e)

    # Verify Milvus
    try:
        milvus_connections.connect(alias="default", uri=settings.milvus_uri)
        logger.info("Milvus connected at %s", settings.milvus_uri)
    except Exception as e:
        logger.warning("Milvus not reachable at %s: %s", settings.milvus_uri, e)

    # Database connectivity is verified by first request via get_db()
    logger.info("Scaffold Engine starting — log_level=%s", settings.log_level)

    yield

    # Shutdown
    milvus_connections.disconnect("default")
    logger.info("Scaffold Engine stopped")


app = FastAPI(
    dependencies=[Depends(require_api_key)],
    title="Scaffold Engine",
    description="Self-hosted RAG-powered workflow orchestrator",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(ErrorLoggingMiddleware)
app.add_middleware(PerformanceMiddleware)


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


@app.get("/health")
async def health():
    """Liveness check — returns service status and connectivity."""
    checks = {"orchestrator": "ok", "ollama": "unknown", "milvus": "unknown", "postgres": "unknown"}

    # Ollama
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(f"{settings.ollama_base_url}/api/tags")
            checks["ollama"] = "ok" if resp.status_code == 200 else f"http_{resp.status_code}"
    except Exception as e:
        checks["ollama"] = f"error: {e}"

    # Milvus
    try:
        from pymilvus import utility
        utility.list_collections()
        checks["milvus"] = "ok"
    except Exception as e:
        checks["milvus"] = f"error: {e}"

    # PostgreSQL
    try:
        from sqlalchemy import text
        from app.database import engine
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        checks["postgres"] = "ok"
    except Exception as e:
        checks["postgres"] = f"error: {e}"

    all_ok = all(v == "ok" for v in checks.values())
    return {"status": "healthy" if all_ok else "degraded", "checks": checks}


# === Endpoint stubs — each will be implemented as a separate module ===

from pydantic import BaseModel
from app.database import get_db
from sqlalchemy.ext.asyncio import AsyncSession
from app.modules.idea_refinement import refine_idea

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
    from sqlalchemy import text
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


from app.modules.dag_generator import generate_dag as _generate_dag

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
        logger.error(f"/gt/list failed: {e}")
        return {"error": str(e)}

@app.post("/gt/search")
async def gt_search_endpoint(body: GtSearchInput):
    """Step 19: Semantic search TOON entries."""
    try:
        return await gt_search(query=body.query, top_k=body.top_k)
    except Exception as e:
        logger.error(f"/gt/search failed: {e}")
        return {"error": str(e)}

@app.get("/gt/detail/{entry_id}")
async def gt_detail_endpoint(entry_id: str):
    """Step 19: Full content of a specific TOON entry."""
    try:
        return await gt_detail(entry_id=entry_id)
    except Exception as e:
        logger.error(f"/gt/detail failed: {e}")
        return {"error": str(e)}

@app.get("/gt/stats")
async def gt_stats_endpoint():
    """Step 19: Collection summary."""
    try:
        return await gt_stats()
    except Exception as e:
        logger.error(f"/gt/stats failed: {e}")
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

@app.get("/status")
async def list_jobs():
    """List active/recent jobs."""
    return {"status": "not_implemented"}


@app.get("/logs")
async def get_logs():
    """Retrieve execution/error/performance logs."""
    return {"status": "not_implemented"}

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

from app.modules.execution_agent import execute_next_node, skip_node, retry_failed_node, execute_all_nodes
from app.schemas import ExecuteNextInput, SkipNodeInput, ExecutionResult

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
