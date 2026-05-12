"""Scaffold Engine — FastAPI orchestrator."""

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from uuid import UUID

import httpx
from fastapi import FastAPI, Request, HTTPException, Depends, UploadFile, File, Query
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
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
from app.middleware.body_size_limit import BodySizeLimitMiddleware
from app.middleware.error_logging import ErrorLoggingMiddleware
from app.middleware.performance import PerformanceMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.modules.dag_generator import generate_dag as _generate_dag
from app.modules.execution_agent import execute_next_node, skip_node, retry_failed_node, execute_all_nodes
from app.modules.execution_handler import execution_status, resume_cancelled_job
from app.modules.gt_browser import gt_list, gt_search, gt_detail, gt_stats
from app.modules.gt_extractor import extract_ground_truths
from app.modules.idea_refinement import refine_idea
from app.modules.ideation_workflow import analyze_and_confirm, research_and_compile
from app.modules.research_agent import run_research, run_research_pdf, resume_research
from app.modules.prompt_inspector import list_prompts, get_prompt, update_prompt, get_history
from app.modules.prompt_optimizer import optimize_prompt
from app.modules.rag_pipeline import query_rag as _query_rag
from app.routers.alerts import router as alerts_router
from app.routers.assist import router as assist_router
from app.routers.observability import router as observability_router
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
    ResumeJobInput,
    ScheduleCreate,
    ScheduleResponse,
    SkipNodeInput,
    JobCostsBreakdownItem,
    JobCostsResponse,
    JobRenameInput,
    JobSynthesisOverrideInput,
    JobSynthesisOverrideResponse,
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


def _check_reranker_state(state) -> dict:
    """Sprint X.1: surface lifespan prewarm outcome to /health.

    Status:
      - "up"       — prewarm completed (state.reranker_prewarmed_at set)
      - "down"     — prewarm errored (state.reranker_prewarm_error set)
      - "skipped"  — SCAFFOLD_PREWARM_RERANKER=false at boot
      - "unknown"  — neither flag present (build pre-X.1, or app.state
                     not yet wired). Treated as non-fatal.

    Pulled out of health() to keep it directly unit-testable. ``state``
    is the FastAPI app's state object (or any object with the same
    attribute names).
    """
    if state is None:
        return {"status": "unknown", "prewarmed": False}
    prewarmed_at = getattr(state, "reranker_prewarmed_at", None)
    elapsed = getattr(state, "reranker_prewarm_elapsed_s", None)
    error = getattr(state, "reranker_prewarm_error", None)
    skipped = getattr(state, "reranker_prewarm_skipped", False)
    if error:
        return {"status": "down", "prewarmed": False, "error": error}
    if skipped:
        return {"status": "skipped", "prewarmed": False}
    if prewarmed_at:
        return {
            "status": "up", "prewarmed": True,
            "prewarmed_at": prewarmed_at, "elapsed_s": elapsed,
        }
    return {"status": "unknown", "prewarmed": False}


async def _pre_migration_sweep() -> dict:
    """Idempotent startup crash-recovery sweep.

    Two independent stages, both run unconditionally; either is a no-op
    on a fresh or healthy DB:

    1. **research_sessions** — cancel any ``'running'`` row older than
       5 minutes. Audit item 7: makes migration 020's UNIQUE-index
       precondition robust regardless of when 020 first applies and
       regardless of crash-recovery state.
    2. **dag_nodes** — reset any ``'running'`` row to ``'pending'`` and
       refresh ``updated_at`` on the owning jobs. No time threshold:
       at lifespan startup the orchestrator process is the only one that
       could have set a node to 'running', so any such row is by
       definition a crash-orphan (X.25, see below).

    Returns the legacy three-key shape for back-compat with the lifespan
    log (``skipped`` / ``reason`` / ``cleared`` describe stage 1) plus
    additive keys for stage 2 (``dag_nodes_reset``, ``parent_jobs_refreshed``).

    Sprint X.1: research_sessions cutoff tightened 30min → 5min once
    ``_sse_with_disconnect_watch`` reliably finalized rows live.

    Sprint X.25: stage 2 added. Previously dag_nodes relied on the 30-min
    periodic orphan reaper (``_REAP_ORPHAN_NODES_SQL``), which used
    ``started_at < NOW() - threshold`` — correct under live operation but
    leaving up to a 30-min dead window after a crash where nodes sat
    'running' and ``_REAP_RUNNING_SQL`` (which refuses to fail jobs with
    a running node) couldn't reap the parents. Resetting at startup with
    no threshold closes the window: any 'running' node at startup is an
    orphan because no executor exists yet.
    """
    async with async_session() as db:
        async with db.begin():
            sessions_exist = await db.execute(text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'research_sessions'"
            ))
            if sessions_exist.scalar() is None:
                sessions_skipped = True
                sessions_reason = "table_not_yet_created"
                sessions_cleared = 0
            else:
                sessions_result = await db.execute(text("""
                    UPDATE research_sessions
                       SET status = 'cancelled',
                           error_message = COALESCE(error_message, 'reaped_at_startup'),
                           completed_at = NOW(),
                           updated_at = NOW()
                     WHERE status = 'running'
                       AND updated_at < NOW() - INTERVAL '5 minutes'
                """))
                sessions_skipped = False
                sessions_reason = None
                sessions_cleared = (
                    sessions_result.rowcount
                    if sessions_result.rowcount is not None else 0
                )

            nodes_exist = await db.execute(text(
                "SELECT 1 FROM information_schema.tables "
                "WHERE table_name = 'dag_nodes'"
            ))
            if nodes_exist.scalar() is None:
                dag_nodes_reset = 0
                parent_jobs_refreshed = 0
            else:
                nodes_result = await db.execute(text("""
                    UPDATE dag_nodes
                       SET status = 'pending',
                           updated_at = NOW()
                     WHERE status = 'running'
                    RETURNING job_id
                """))
                node_rows = nodes_result.fetchall()
                dag_nodes_reset = len(node_rows)
                parent_jobs_refreshed = 0
                if node_rows:
                    affected_job_ids = list({str(r.job_id) for r in node_rows})
                    refresh_result = await db.execute(text("""
                        UPDATE jobs
                           SET updated_at = NOW()
                         WHERE id = ANY(CAST(:ids AS uuid[]))
                           AND status IN ('running', 'executing')
                        RETURNING id
                    """), {"ids": affected_job_ids})
                    parent_jobs_refreshed = len(refresh_result.fetchall())

            return {
                "skipped": sessions_skipped,
                "reason": sessions_reason,
                "cleared": sessions_cleared,
                "dag_nodes_reset": dag_nodes_reset,
                "parent_jobs_refreshed": parent_jobs_refreshed,
            }


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

    # Defensive pre-migration sweep (audit item 7): clear any 'running'
    # research_sessions older than 30 min so migration 020's UNIQUE-index
    # precondition is robust regardless of when 020 first applies and
    # regardless of crash-recovery state. Idempotent across all DB ages.
    # No-op on fresh DBs where research_sessions doesn't exist yet
    # (created by migration 010); also doubles as crash-recovery on
    # established DBs that died mid-execution with stuck 'running' rows.
    try:
        sweep = await _pre_migration_sweep()
        if sweep["skipped"]:
            logger.info(
                "startup_sweep_skipped: reason=%s dag_nodes_reset=%d parent_jobs_refreshed=%d",
                sweep["reason"], sweep["dag_nodes_reset"], sweep["parent_jobs_refreshed"],
            )
        else:
            logger.info(
                "startup_sweep_complete: stale_running_cleared=%d "
                "dag_nodes_reset=%d parent_jobs_refreshed=%d",
                sweep["cleared"], sweep["dag_nodes_reset"], sweep["parent_jobs_refreshed"],
            )
    except Exception as exc:
        # Keep this defensive — sweep failure must not block startup since
        # the migration runner has its own error handling we still want
        # to reach.
        logger.warning("startup_sweep_failed: error=%s", exc)

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

    # §17.135 — Embedder-identity drift detection. Must run AFTER the
    # migration runner (we need the cache_metadata table) but BEFORE any
    # path that exercises the embedder. Fail-soft: a DB hiccup logs but
    # does not crash startup; the drift just goes unnoticed until next
    # boot.
    try:
        from app.database import async_session
        from app.utils.embedder_drift import check_embedder_drift
        async with async_session() as _drift_db:
            drift_result = await check_embedder_drift(_drift_db)
        if drift_result.get("outcome") == "drift":
            logger.critical(
                "lifespan_embedder_drift: stored=%s configured=%s",
                drift_result.get("stored"), drift_result.get("current"),
            )
        elif drift_result.get("outcome") != "skipped":
            logger.info(
                "embedder_identity_check: outcome=%s model=%s",
                drift_result.get("outcome"),
                drift_result.get("current"),
            )
    except Exception as exc:
        logger.warning("embedder_drift_hook_failed: err=%s", exc)

    # §17.138 — Embedding-cache L1 warmup from L2 (Redis). Runs after the
    # drift check so we never warm L1 with keys for a model we're about
    # to invalidate. Fail-soft: a Redis hiccup logs but doesn't block
    # startup; the cache just starts cold (the prior behavior).
    if settings.embedding_cache_warmup_n > 0:
        try:
            from app.utils.embedding_cache import get_cache
            warmup_result = await get_cache().warmup()
            logger.info(
                "embedding_cache_warmup_done: loaded=%d skipped=%d scanned=%d",
                warmup_result["loaded"],
                warmup_result["skipped"],
                warmup_result["scanned"],
            )
        except Exception as exc:
            logger.warning("embedding_cache_warmup_hook_failed: err=%s", exc)

    logger.info("engine_started: log_level=%s", settings.log_level)
    # Eager-init shared HTTP clients (searxng, github, generic) — no lazy path
    from app.utils.http_clients import init_clients
    init_clients()

    # Pre-warm reranker (Apr 26 2026): avoid ~13s cold-load on first user request.
    # Opt out: SCAFFOLD_PREWARM_RERANKER=false
    # Sprint X.1: surface prewarm outcome on app.state so /health can
    # report it. This makes a silent prewarm failure (which previously
    # only logged a WARNING) operator-visible.
    app.state.reranker_prewarmed_at = None
    app.state.reranker_prewarm_elapsed_s = None
    app.state.reranker_prewarm_error = None
    app.state.reranker_prewarm_skipped = False
    if os.getenv("SCAFFOLD_PREWARM_RERANKER", "true").strip().lower() not in ("0", "false", "no", "off"):
        try:
            import asyncio
            import time as _time
            from datetime import datetime, timezone
            from app.rerankers import _get_cross_encoder
            loop = asyncio.get_running_loop()
            _t0 = _time.monotonic()
            await loop.run_in_executor(None, _get_cross_encoder)
            elapsed = round(_time.monotonic() - _t0, 2)
            app.state.reranker_prewarmed_at = datetime.now(timezone.utc).isoformat()
            app.state.reranker_prewarm_elapsed_s = elapsed
            logger.info("reranker_prewarmed")
        except Exception as exc:
            app.state.reranker_prewarm_error = str(exc)
            logger.warning("reranker_prewarm_failed: %s", exc)
    else:
        app.state.reranker_prewarm_skipped = True

    # Eager cleanup at startup. Default-on (X.25): the periodic reaper's
    # first sweep is gated by `cleanup_interval_seconds` (15 min default),
    # so without an explicit eager pass any non-orphan stale state
    # (e.g. long-phase jobs whose updated_at is already past threshold)
    # waits one full interval before being reaped. Combined with the
    # `_pre_migration_sweep` dag_nodes reset above, this closes the
    # restart-mid-DAG dead window. Opt out with CLEANUP_ON_STARTUP=false.
    _cleanup_startup = os.getenv("CLEANUP_ON_STARTUP", "true").strip().lower()
    if _cleanup_startup not in ("0", "false", "no", "off"):
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
    else:
        logger.info('event="startup_cleanup_skipped" CLEANUP_ON_STARTUP=%s', _cleanup_startup)

    _cleanup_task = start_cleanup_task()
    # Start APScheduler (rehydrates scheduled_jobs from DB; X.26 also
    # registers the threshold-eval + calibration-watchdog interval jobs).
    try:
        from app.scheduler import init_scheduler
        await init_scheduler()
    except Exception as exc:
        logger.error('event="scheduler_init_failed" error=%s', exc)

    # Sprint X.26 — OTel init is strictly opt-in and a no-op unless
    # `otel_enabled` + an OTLP HTTP endpoint are configured. Failures
    # log a warning and continue; tracing is never load-bearing.
    try:
        from app.observability.otel import init_tracing
        init_tracing(app)
    except Exception as exc:
        logger.warning('event="otel_init_top_level_failed" error=%s', exc)
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
    version="1.1.0",
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
# §17.97 — BodySize sits between Performance + RequestId so it sees the
# bound request_id (for the 413 log line) and rejects oversized payloads
# BEFORE Performance times an unnecessarily-long request.
app.add_middleware(BodySizeLimitMiddleware)
app.add_middleware(RequestIdMiddleware)
# §17.97 — SecurityHeaders is OUTERMOST so CSP + nosniff + Referrer-Policy
# wrap the final response right before client send. Set-here-only semantics
# (uses setdefault) so a future per-endpoint header override still wins.
app.add_middleware(SecurityHeadersMiddleware)
app.include_router(status_router)
app.include_router(assist_router)
app.include_router(observability_router)
app.include_router(alerts_router)


# Sprint X.26 — Prometheus exposition. No auth (Prometheus scrapers
# don't carry our X-API-Key header by convention; the surface is
# read-only counters/gauges with no PII), consistent with /health.
@app.get(settings.metrics_path, dependencies=[], include_in_schema=False)
async def metrics_endpoint(request: Request):
    """Sprint X.26 — Prometheus scrape endpoint. Returns 404 when
    `metrics_enabled` is False so operators can disable the surface
    without unmounting the route. The X-API-Key gate is bypassed so a
    Prometheus instance can scrape with the default `/metrics` config."""
    if not settings.metrics_enabled:
        raise HTTPException(status_code=404)
    from app.observability.metrics import expose
    return await expose(request)

# Sprint J.2.a — native single-page web UI. Auth-bypassed so a browser
# hitting localhost:8000/web/jobs works without sending headers; the
# embedded SDK Client carries settings.scaffold_api_key for the loopback
# request to the same orchestrator's API surface.
from app.web.routes import router as web_router  # noqa: E402
app.include_router(web_router, dependencies=[])
app.mount("/static", StaticFiles(directory="app/static"), name="static")


@app.get("/", dependencies=[], include_in_schema=False)
async def web_root_redirect():
    """Sprint J.2.a — redirect ``GET /`` to the web UI's jobs list.

    Excluded from OpenAPI (``include_in_schema=False``) because it's a
    convenience landing for browsers, not a stable API contract.
    """
    return RedirectResponse(url="/web/jobs", status_code=302)


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
        verifier_cache_stats: dict = {}
        rag_cache_stats: dict = {}
        fetch_cache_stats: dict = {}
        try:
            from app.utils.embedding_cache import get_cache
            from app.utils.fetch_cache import get_fetch_cache
            from app.utils.llm_response_cache import get_verifier_cache
            from app.utils.rag_result_cache import get_rag_result_cache
            cache = get_cache()
            cache_stats = cache.stats
            verifier_cache_stats = get_verifier_cache().stats()
            rag_cache_stats = get_rag_result_cache().stats()
            fetch_cache_stats = get_fetch_cache().stats()
            redis_conn = await cache._get_redis()
            await asyncio.wait_for(redis_conn.ping(), timeout=2.0)
            key_count = await asyncio.wait_for(redis_conn.dbsize(), timeout=2.0)
            return ({"status": "up", "keys": key_count},
                    cache_stats, verifier_cache_stats,
                    rag_cache_stats, fetch_cache_stats)
        except Exception:
            return ({"status": "down", "keys": 0},
                    cache_stats, verifier_cache_stats,
                    rag_cache_stats, fetch_cache_stats)

    # Each _check_* wraps its body in try/except Exception and returns a
    # dict on failure, so gather() cannot surface Exception objects from
    # these tasks; ``return_exceptions=True`` is left in only as
    # belt-and-suspenders for BaseException-derived cases (which we'd
    # actually want to propagate, not absorb).
    pg, ollama, milvus, redis_pair = await asyncio.gather(
        _check_pg(), _check_ollama(), _check_milvus(), _check_redis(),
        return_exceptions=True,
    )
    redis_info, cache_stats, verifier_cache_stats, rag_cache_stats, fetch_cache_stats = redis_pair
    reranker = _check_reranker_state(getattr(app, "state", None))
    checks = {
        "postgresql": pg, "ollama": ollama, "milvus": milvus,
        "redis": redis_info, "embedding_cache": cache_stats,
        "verifier_cache": verifier_cache_stats,
        "rag_result_cache": rag_cache_stats,
        "fetch_cache": fetch_cache_stats,
        "reranker": reranker,
    }
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
        # §17.96 — surface the auth posture so operators (and `make doctor`)
        # can spot a SCAFFOLD_AUTH_DISABLED=true deployment without having
        # to grep boot logs. True means the X-API-Key gate is in force;
        # False means it's bypassed (the explicit opt-out flag is set in
        # the env or .env). This field is intentionally exposed on the
        # unauthenticated /health endpoint — it carries no secret, and the
        # whole point of the surfacing is to catch misconfiguration that
        # an attacker could already detect by trying any non-/health URL.
        "auth_enabled": not settings.scaffold_auth_disabled,
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


_CONFIG_REDACT_KEYWORDS = ("key", "secret", "token", "password", "pass")
# URL-with-embedded-credentials: scheme://user:pass@host…
_CONFIG_URL_CREDS_RE = re.compile(r"^[a-z][a-z0-9+\-.]*://[^/@\s]+:[^/@\s]+@")


def _is_secret_field(name: str, value: object) -> bool:
    """True when a Settings field's value should be redacted in /config.

    Three triggers, in priority order:
      1. The field type is ``SecretStr``.
      2. The field NAME contains a sensitive keyword (``key`` / ``secret``
         / ``token`` / ``password`` / ``pass``).
      3. The field VALUE is a URL with embedded user:password credentials
         (e.g. ``postgresql+asyncpg://scaffold:abcd@host:5432/db``) —
         catches ``database_url`` and similar without false-positiving on
         credential-free URLs like ``http://172.18.0.1:11434``.
    We err on the side of over-redaction rather than leaking values
    via the public API.
    """
    from pydantic import SecretStr
    if isinstance(value, SecretStr):
        return True
    lname = name.lower()
    if any(kw in lname for kw in _CONFIG_REDACT_KEYWORDS):
        return True
    if isinstance(value, str) and _CONFIG_URL_CREDS_RE.match(value):
        return True
    return False


@app.get("/config", tags=["ops"])
async def get_config():
    """Return the orchestrator's loaded Settings (audit item U.5).

    Sensitive fields (SecretStr-typed or with names matching `key` /
    `secret` / `token` / `password` / `pass`) are redacted to either
    `(set)` or `(unset)` so the response can be safely shown in CLI
    output and pasted into bug reports.

    Each field carries the field name, current value (or redaction),
    type, default, whether the runtime value matches the default,
    and the field's docstring (extracted from app/config.py field
    descriptions where available).

    Requires API key (inherited from global auth).
    """
    from app.config import Settings, settings as _live_settings

    fields_meta = Settings.model_fields
    out: list[dict] = []
    for name, finfo in fields_meta.items():
        live_value = getattr(_live_settings, name)
        default = finfo.default
        type_repr = str(finfo.annotation).replace("typing.", "")

        if _is_secret_field(name, live_value):
            display = "(set)" if live_value else "(unset)"
            display_default = "(redacted)"
        else:
            # Convert dicts/lists/etc to JSON-safe primitives.
            try:
                display = live_value if isinstance(live_value, (str, int, float, bool)) else str(live_value)
                display_default = default if isinstance(default, (str, int, float, bool, type(None))) else str(default)
            except Exception:
                display = str(live_value)
                display_default = "?"

        out.append({
            "name": name,
            "value": display,
            "type": type_repr,
            "default": display_default,
            "is_default": live_value == default,
            "description": finfo.description or "",
        })

    return {
        "fields": out,
        "redacted": [f["name"] for f in out if "(set)" in str(f["value"]) or "(unset)" in str(f["value"])],
        "count": len(out),
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
        query_intent=body.query_intent,
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


@app.post("/jobs/{job_id}/resume", tags=["Management"])
async def resume_job_endpoint(
    job_id: str,
    body: ResumeJobInput,
    db: AsyncSession = Depends(get_db),
):
    """Resume a cancelled job and stream its execution.

    Atomically flips the job from ``cancelled`` back to ``executing`` and
    re-fires ``/execute/all``-equivalent execution. Replaces the manual
    SQL-then-curl recipe in debugging.md. ``execute_all_nodes`` is
    idempotent over completed nodes — execution picks up from the last
    pending node, with done-node outputs serving as upstream context.

    Status codes:
      - 200 + SSE stream on successful resume
      - 404 if no job with that ID exists
      - 409 if the job exists but isn't in ``cancelled`` (current status
        returned in detail for client-side dispatch)
      - 400 on malformed UUID
    """
    try:
        parsed_id = UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=400, detail="Invalid job_id format")

    await _require_valid_models(body.model_overrides)
    outcome = await resume_cancelled_job(parsed_id, db)

    if outcome["outcome"] == "not_found":
        raise HTTPException(status_code=404, detail=f"Job {job_id} not found")
    if outcome["outcome"] == "wrong_status":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "job not resumable",
                "current_status": outcome["current_status"],
                "expected_status": "cancelled",
            },
        )
    # outcome == "resumed" — start streaming.
    return StreamingResponse(
        execute_all_nodes(job_id, model_overrides=body.model_overrides),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
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


@app.get("/research/verify/{session_id}", tags=["Research"])
async def research_verify_endpoint(
    session_id: str,
    recheck: bool = Query(False, description="If true, HEAD-request each entry's source_url to surface upstream reachability state."),
    compare_hash: bool = Query(False, description="If true (§17.126), GET each URL and SHA256-compare against the stored raw_upstream_hash. Implies recheck=true."),
):
    """Session-scoped provenance audit (§17.114 + §17.121).

    Lists every Milvus entry produced by the given research session and
    reports its current state — present, superseded, or missing. Used to
    surface drift between what was ingested and what's currently in the
    index, without re-fetching upstream content. See
    ``app/modules/research_verify.py`` for the returned-shape contract.

    ``?recheck=true`` (§17.121) additionally HEAD-requests each entry's
    ``source_url`` and reports ``upstream_state`` (reachable / missing /
    forbidden / error / skipped) per entry plus rollup totals. Bounded
    concurrency (5). SSRF re-checked on every URL.

    Pre-§17.114 sessions have no provenance rows linked by session_id
    and return an empty ``entries`` list — that's expected, not an error.
    """
    from uuid import UUID
    from app.database import async_session
    from app.modules.research_verify import verify_session

    try:
        UUID(session_id)
    except (ValueError, TypeError):
        from fastapi import HTTPException
        raise HTTPException(status_code=400, detail=f"Invalid session_id (must be UUID): {session_id!r}")

    async with async_session() as db_session:
        return await verify_session(
            db_session, session_id,
            recheck_upstream=recheck,
            compare_hash=compare_hash,
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
    synthesized: bool | None = None,
    limit: int = 25,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
):
    """Paginated job list with optional status filter and title search.

    Sprint X.9 — ``synthesized`` filter complements the X.6 per-job opt-in:
    ``?synthesized=true`` lists only jobs whose ``compiled_output`` was
    LLM-synthesized (W.7 narrative pass); ``?synthesized=false`` lists
    everything else (heuristic compile, unsynthesized, or not-yet-compiled).
    Omit the param to see all jobs.
    """
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
    if synthesized is not None:
        where_clauses.append("j.compiled_output_synthesized = :synthesized")
        params["synthesized"] = synthesized

    # SAFE: where_clauses contain only bind-parameter placeholders (:status, :q,
    # :synthesized); all user values flow through `params` dict. Do not
    # interpolate user input into where_clauses directly without enum/whitelist
    # validation first.
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
    error_logs (FK ON DELETE CASCADE). llm_call_logs rows are unaffected
    (no FK; off-job calls live there too)."""
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


@app.get(
    "/jobs/{job_id}/costs",
    response_model=JobCostsResponse,
    tags=["Management"],
)
async def get_job_costs_endpoint(
    job_id: str,
    db: AsyncSession = Depends(get_db),
):
    """Sprint J.3.b — aggregate cost + latency for a job.

    Returns total USD spent (computed at insert time, immutable),
    total prompt/completion tokens, total LLM latency, the count of
    LLM calls logged for this job, and a per-(provider, model)
    breakdown sorted by descending cost. Job_ids with no logged
    calls return the zero shape with an empty breakdown — fail-open
    matches the rest of the cost-tracking surface.
    """
    try:
        UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="job_id must be a valid UUID")

    from app.modules.cost_rollup import get_job_costs
    payload = await get_job_costs(job_id, db)
    return JobCostsResponse(
        job_id=payload["job_id"],
        total_cost_usd=payload["total_cost_usd"],
        total_prompt_tokens=payload["total_prompt_tokens"],
        total_completion_tokens=payload["total_completion_tokens"],
        total_latency_ms=payload["total_latency_ms"],
        call_count=payload["call_count"],
        by_provider=[JobCostsBreakdownItem(**row) for row in payload["by_provider"]],
    )


@app.patch(
    "/jobs/{job_id}/synthesis",
    response_model=JobSynthesisOverrideResponse,
    tags=["Management"],
)
async def set_job_synthesis_override(
    job_id: str,
    body: JobSynthesisOverrideInput,
    db: AsyncSession = Depends(get_db),
):
    """Sprint X.6 — set the per-job opt-in for the W.7 LLM synthesis pass.

    Body ``{"override": true}`` forces synthesis on for this job;
    ``{"override": false}`` forces it off; ``{"override": null}`` clears
    the override so the job inherits ``settings.compile_synthesis_enabled``.

    The override is read by ``execution_compile._resolve_synthesis_enabled``
    on the next compile pass — set it before ``/execute/all`` (or before
    a final-node retry) for it to take effect on the resulting deliverable.
    """
    try:
        UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        raise HTTPException(status_code=422, detail="job_id must be a valid UUID")

    r = await db.execute(text("""
        UPDATE jobs
           SET compile_synthesis_override = :override,
               updated_at = NOW()
         WHERE id = :id
        RETURNING id, compile_synthesis_override
    """), {"id": job_id, "override": body.override})
    row = r.fetchone()
    if not row:
        raise HTTPException(status_code=404, detail=f"job not found: {job_id}")
    await db.commit()
    return JobSynthesisOverrideResponse(
        job_id=str(row.id),
        override=row.compile_synthesis_override,
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
