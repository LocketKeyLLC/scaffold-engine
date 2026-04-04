"""
Task 1: Health Check Endpoint — GET /health

Drop this file into: ~/scaffold-engine/app/routers/health.py

Integration:
    In app/main.py, add:
        from app.routers.health import router as health_router
        app.include_router(health_router)

Verification:
    # All services up:
    curl -s http://localhost:8000/health | python3 -m json.tool

    # Expected shape:
    # {
    #   "status": "healthy",
    #   "timestamp": "2026-04-04T...",
    #   "checks": {
    #     "postgresql": {"status": "up", "latency_ms": 12},
    #     "ollama":     {"status": "up", "latency_ms": 45, "models_loaded": [...]},
    #     "milvus":     {"status": "up", "latency_ms": 30, "collection_count": 1, "entry_count": 83}
    #   }
    # }

    # With a service down:
    docker stop scaffold-postgres
    curl -s http://localhost:8000/health | python3 -m json.tool
    # status should be "unhealthy" (postgres is critical)
    docker start scaffold-postgres

Rollback:
    Remove the file and remove the include_router() line from main.py.
"""

import asyncio
import logging
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter
from pymilvus import connections, utility, Collection

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ops"])

CHECK_TIMEOUT = 5.0  # seconds per dependency


# ── Individual checks ────────────────────────────────────────────────

async def _check_postgresql() -> dict:
    """SELECT 1 against scaffold-postgres via asyncpg (or SQLAlchemy async)."""
    t0 = time.monotonic()
    try:
        # Attempt asyncpg first (lighter weight for a health probe).
        # If the project uses SQLAlchemy exclusively, swap this block
        # for a session.execute(text("SELECT 1")) call.
        import asyncpg  # type: ignore

        conn = await asyncio.wait_for(
            asyncpg.connect(
                host="scaffold-postgres",
                port=5432,
                user="scaffold",       # ← verify against your .env / settings
                password="scaffold",   # ← verify against your .env / settings
                database="scaffold",   # ← verify against your .env / settings
            ),
            timeout=CHECK_TIMEOUT,
        )
        try:
            await conn.fetchval("SELECT 1")
        finally:
            await conn.close()

        latency = round((time.monotonic() - t0) * 1000)
        return {"status": "up", "latency_ms": latency}

    except Exception as exc:
        latency = round((time.monotonic() - t0) * 1000)
        logger.warning("health_check_failed component=postgresql error=%s", exc)
        return {"status": "down", "latency_ms": latency}


async def _check_ollama() -> dict:
    """GET /api/tags from Ollama to confirm it's alive and list loaded models."""
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT) as client:
            resp = await client.get("http://172.18.0.1:11434/api/tags")
            resp.raise_for_status()
            data = resp.json()
            models = [m["name"] for m in data.get("models", [])]
        latency = round((time.monotonic() - t0) * 1000)
        return {"status": "up", "latency_ms": latency, "models_loaded": models}
    except Exception as exc:
        latency = round((time.monotonic() - t0) * 1000)
        logger.warning("health_check_failed component=ollama error=%s", exc)
        return {"status": "down", "latency_ms": latency, "models_loaded": []}


async def _check_milvus() -> dict:
    """List collections + get entry count from the primary collection."""
    t0 = time.monotonic()
    try:
        # pymilvus is sync — run in executor to avoid blocking the loop.
        loop = asyncio.get_running_loop()
        result = await asyncio.wait_for(
            loop.run_in_executor(None, _milvus_sync_check),
            timeout=CHECK_TIMEOUT,
        )
        result["latency_ms"] = round((time.monotonic() - t0) * 1000)
        return result
    except Exception as exc:
        latency = round((time.monotonic() - t0) * 1000)
        logger.warning("health_check_failed component=milvus error=%s", exc)
        return {"status": "down", "latency_ms": latency, "collection_count": 0, "entry_count": 0}


def _milvus_sync_check() -> dict:
    """Synchronous Milvus probe (runs in thread via executor)."""
    alias = "_health_check"
    try:
        connections.connect(alias=alias, host="milvus-standalone", port="19530")
        colls = utility.list_collections(using=alias)
        entry_count = 0
        # Use the known collection name; fall back to first collection.
        target = "technical_knowledge"
        if target in colls:
            col = Collection(target, using=alias)
            col.flush()
            entry_count = col.num_entities
        elif colls:
            col = Collection(colls[0], using=alias)
            col.flush()
            entry_count = col.num_entities
        return {
            "status": "up",
            "collection_count": len(colls),
            "entry_count": entry_count,
        }
    finally:
        try:
            connections.disconnect(alias)
        except Exception:
            pass


# ── Endpoint ─────────────────────────────────────────────────────────

@router.get("/health")
async def health():
    """
    Concurrent dependency health check.
    No authentication required — designed for external monitors and the
    pipeline container.
    """
    pg, ollama, milvus = await asyncio.gather(
        _check_postgresql(),
        _check_ollama(),
        _check_milvus(),
        return_exceptions=True,
    )

    # If gather returned an exception object, treat as down.
    if isinstance(pg, Exception):
        pg = {"status": "down", "latency_ms": 0}
    if isinstance(ollama, Exception):
        ollama = {"status": "down", "latency_ms": 0, "models_loaded": []}
    if isinstance(milvus, Exception):
        milvus = {"status": "down", "latency_ms": 0, "collection_count": 0, "entry_count": 0}

    checks = {
        "postgresql": pg,
        "ollama": ollama,
        "milvus": milvus,
    }

    # Determine aggregate status.
    pg_up = pg["status"] == "up"
    ollama_up = ollama["status"] == "up"
    milvus_up = milvus["status"] == "up"

    if pg_up and ollama_up and milvus_up:
        status = "healthy"
    elif pg_up and ollama_up:
        # Milvus down → degraded (non-critical for basic operation)
        status = "degraded"
    else:
        status = "unhealthy"

    return {
        "status": status,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
    }
