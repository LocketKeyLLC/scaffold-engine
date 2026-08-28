"""Scaffold Engine — FastAPI orchestrator.

§17.174 — most endpoints have been extracted into per-domain routers
under ``app/routers/``. main.py keeps only the lifespan, middleware,
and the system endpoints (``/health``, ``/config``, ``/metrics``,
``/``). The full list of moved endpoints is at the bottom of this
file just before the ``app.include_router`` calls.
"""

import asyncio
import logging
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# §17.203 — response_model annotations for /health, /config, /rag/dedup
# (AUDIT 3.7). Pinning the top-level shapes for SDK consumers.
from app.schemas import (
    ConfigResponse,
    HealthCheckResponse,
)
from app.utils.milvus_utils import (
    get_client as get_milvus_client,
    close_client as close_milvus_client,
)
from sqlalchemy import text
from app.model_router import close_client

from app.auth import require_api_key
from app.authz import require_admin
from app.config import SWITCHABLE_ROLE_FIELDS, settings
from app.modules.cleanup import start_cleanup_task, reap_stale_jobs
from app.database import engine, async_session
from app.logging_config import setup_logging
from app.middleware.body_size_limit import BodySizeLimitMiddleware
from app.middleware.error_logging import ErrorLoggingMiddleware
from app.middleware.performance import PerformanceMiddleware
from app.middleware.request_id import RequestIdMiddleware
from app.middleware.security_headers import SecurityHeadersMiddleware
from app.middleware.web_csrf import WebCsrfMiddleware
from app.routers.alerts import router as alerts_router
from app.routers.assist import router as assist_router
from app.routers.observability import router as observability_router
from app.routers.design import router as design_router
from app.routers.specs import (
    router as specs_router,
    sizing_router,
    report_router,
    digital_report_router,
)
from app.routers.status import router as status_router

logger = logging.getLogger("scaffold")

# §17.812 (audit M3) — records a startup migration failure so it stops being
# silent: /health surfaces it as a warning and the operator sees it without
# grepping logs. None = migrations OK (or not yet run). Set in the lifespan.
_MIGRATION_STATE: dict | None = None

# §17.179 — cap each lifespan service probe at this many seconds. The
# Ollama client default (1800 s = local_timeout, sized for LLM calls),
# pymilvus's unbounded connect, and asyncpg's 60 s connect_timeout were
# all longer than any sensible startup budget.
#
# §17.179 follow-up (2026-05-23) — lowered from 5.0 → 2.0. The §17.179
# OVERVIEW entry already flagged 2 s as the right long-term cap; 5 s was
# the conservative first step. Cloud-CI smoke runs against unreachable
# scaffold-postgres / milvus / ollama still complete the lifespan inside
# pytest's 30 s timeout with margin (3 probes × 2 s + remaining steps).
# Healthy localhost handshakes complete in <50 ms — 2 s is 40× headroom.
_STARTUP_PROBE_TIMEOUT_S: float = 2.0

setup_logging(
    json_logs=settings.log_json_format,
    log_level=settings.log_level,
    log_file=settings.log_file,
)


# §17.855 (audit B7) — /health probe logic lives in app/health.py.
# Re-exported here so `from app.main import _check_reranker_state /
# _model_role_warnings / health` and the existing tests keep working.
from app.health import (  # noqa: E402
    _check_reranker_state,
    _model_role_warnings,
    build_health_response,
)


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

    §17.198: the 5-minute cutoff is now configurable via
    ``settings.startup_sweep_research_idle_min`` (default 5). An operator
    restarting the orchestrator during a slow LLM call (the 7b verifier's
    cold-load can take 6+ minutes) can raise the cutoff so the in-flight
    row doesn't get reaped mid-flight. Floor is 1 minute (any lower and
    healthy rows get reaped); ceiling is 24h.

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
                # §17.198 — interval driven by settings, not hardcoded.
                # Postgres' make_interval handles the minute-level value
                # cleanly without f-string SQL interpolation (which would
                # be unsafe for arbitrary values; this one is bounded by
                # the settings field's ge/le but we still parameterize).
                sessions_result = await db.execute(text("""
                    UPDATE research_sessions
                       SET status = 'cancelled',
                           error_message = COALESCE(error_message, 'reaped_at_startup'),
                           completed_at = NOW(),
                           updated_at = NOW()
                     WHERE status = 'running'
                       AND updated_at < NOW() - make_interval(mins => :idle_min)
                """), {"idle_min": settings.startup_sweep_research_idle_min})
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
    """Startup: verify Ollama, Milvus, PostgreSQL connectivity.

    §17.179 — all three probes are timeout-capped at
    ``_STARTUP_PROBE_TIMEOUT_S`` (5 s) so the lifespan completes fast
    under unreachable conditions. Pre-§17.179 the Ollama client
    inherited the 1800 s ``local_timeout`` default, Milvus connect had
    no timeout cap, and asyncpg's connect default of 60 s would bite
    every DB-touching step downstream. Cumulatively this could make
    ``with TestClient(app) as c:`` hang for many minutes in cloud-CI
    smoke runs (caught and partially worked around by §17.177/§17.178).
    The cap here is the defensive root-cause fix; the §17.178 test
    refactor stays as belt-and-suspenders.
    """

    # §17.854 (audit B1) — init the shared HTTP clients FIRST. The Ollama probe
    # below uses model_router._get_client(), which has NO lazy path; with the
    # init sitting further down (§17.812 hoisted it above crash-resume only),
    # this probe raised "client not initialized" on every boot and never once
    # reported real Ollama health. init_clients() is a dependency-free
    # singleton init — safe at the very top.
    from app.utils.http_clients import init_clients
    init_clients()

    # Verify Ollama — explicit 5 s cap overrides the client's default
    # local_timeout (1800 s, sized for actual LLM calls).
    try:
        from app.model_router import _get_client
        resp = await _get_client().get(
            f"{settings.ollama_base_url}/api/tags",
            timeout=_STARTUP_PROBE_TIMEOUT_S,
        )
        models = [m["name"] for m in resp.json().get("models", [])]
        logger.info("ollama_connected: models_available=%d", len(models))
    except Exception as e:
        logger.warning("ollama_connection_failed: url=%s error=%s", settings.ollama_base_url, e)

    # Verify Milvus — MilvusClient construction + collection load is sync
    # (§17.591); wrap so the event loop is not blocked during the
    # (potentially slow) initial connect handshake. asyncio.wait_for caps
    # the await; the underlying thread may still be running after timeout
    # but that's acceptable for a one-shot lifespan call (orchestrator is
    # starting up — a zombie thread is harmless until process exit).
    # get_milvus_client() warms + caches the shared client so the first
    # real request doesn't pay the cold-load cost.
    try:
        loop = asyncio.get_running_loop()
        await asyncio.wait_for(
            loop.run_in_executor(None, get_milvus_client),
            timeout=_STARTUP_PROBE_TIMEOUT_S,
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
        global _MIGRATION_STATE
        _mig_failure: str | None = None
        try:
            from app.migrations import run_migrations
            mig_result = await run_migrations()
            if mig_result.get("status") == "error":
                logger.error("migrations_failed_at_startup: %s", mig_result)
                _mig_failure = (
                    f"{mig_result.get('failed_file') or '?'}: "
                    f"{mig_result.get('error') or mig_result}"
                )
            elif mig_result.get("applied"):
                logger.info(
                    "migrations_applied_at_startup: count=%d files=%s",
                    len(mig_result["applied"]), mig_result["applied"],
                )
        except Exception as exc:
            logger.error("migrations_hook_crashed: error=%s", exc)
            _mig_failure = f"migration hook crashed: {exc}"

        # §17.812 (audit M3) — a startup migration failure used to be a single
        # log line: the app booted and served traffic against a PARTIAL schema
        # (every path touching a later table 500s), with no operator-visible
        # signal. Surface it on /health.warnings, fire a system alert, and — when
        # fail_on_migration_error is set — refuse to serve rather than run broken.
        if _mig_failure:
            _MIGRATION_STATE = {"status": "error", "detail": _mig_failure}
            try:
                from app.observability import alerts
                await alerts.emit(
                    kind="migration.failed",
                    severity="critical",
                    message=f"Startup migration failed: {_mig_failure}",
                    dedup_key="migration.failed",
                )
            except Exception as _alert_exc:  # emit never raises, but be defensive
                logger.warning("migration_alert_emit_failed: err=%s", _alert_exc)
            if settings.fail_on_migration_error:
                raise RuntimeError(
                    f"Startup migration failed and fail_on_migration_error is set — "
                    f"refusing to serve on a partial schema: {_mig_failure}"
                )
    else:
        logger.info("migrations_skipped_by_env: SCAFFOLD_RUN_MIGRATIONS_ON_STARTUP=%s", _run_migs)

    # §17.484 — Replay persisted per-role model overrides onto the live
    # settings singleton. Must run AFTER migrations (needs the model_overrides
    # table). Fail-soft: a DB hiccup logs but doesn't block startup — the
    # roles just keep their env/config defaults. Uses the module-level
    # async_session (imported at L36) — no function-local import (§17.164).
    try:
        from app.modules.model_overrides import load_overrides_into_settings
        async with async_session() as _mo_db:
            n_overrides = await load_overrides_into_settings(_mo_db)
        if n_overrides:
            logger.info("model_overrides_applied_at_startup: count=%d", n_overrides)
    except Exception as exc:
        logger.warning("model_overrides_hook_failed: err=%s", exc)

    # §17.809 — Re-apply the active runtime profile's KNOBS onto settings. Must
    # run AFTER the model-overrides replay above: a profile writes its per-role
    # model swaps through set_override (so they ride the hook above), and this
    # only restores the non-model knobs (max_retries, node_escalation, the
    # faithfulness/CoVe/research caps) that a restart reset to env defaults.
    # Fail-soft — a hiccup leaves knobs at env defaults, never blocks startup.
    try:
        from app.modules.profiles import load_profile_into_settings
        async with async_session() as _prof_db:
            active_prof = await load_profile_into_settings(_prof_db)
        if active_prof:
            logger.info("runtime_profile_applied_at_startup: name=%s", active_prof)
    except Exception as exc:
        logger.warning("runtime_profile_hook_failed: err=%s", exc)

    # §17.812 (audit C6) — the shared HTTP clients must be initialized BEFORE
    # crash-resume: resume_orphaned_executions() spawns detached
    # _drain_execution tasks that reach the first LLM/embedder call, and
    # get_ollama_client() has no lazy path. init_clients() now runs at the very
    # top of the lifespan (§17.854), which preserves this ordering.

    # §17.774 — Automatic crash-resume of jobs orphaned mid-execution. Must run
    # AFTER migrations (needs the 061 resume counters) and AFTER the node sweep
    # + model-overrides replay above (so a resumed job's node is 'pending' again
    # and global per-role overrides are live before its drain starts). The
    # startup sweep resets the interrupted node 'running'->'pending' but leaves
    # the parent job 'running'; this re-launches execute_all_nodes for it,
    # resuming at that node and reusing every already-'done' output. Bounded by
    # the crash-loop guard; gated by execution_resume_on_startup_enabled
    # (default on). Fail-soft — a hiccup here must not block startup.
    try:
        from app.modules.execution_resume import resume_orphaned_executions
        resume_result = await resume_orphaned_executions()
        if resume_result["skipped"]:
            logger.info("crash_resume_skipped: reason=%s", resume_result["reason"])
        elif resume_result["resumed"] or resume_result["budget_failed"]:
            logger.info(
                "crash_resume_startup: candidates=%d resumed=%d budget_failed=%d",
                resume_result["candidates"],
                len(resume_result["resumed"]),
                len(resume_result["budget_failed"]),
            )
    except Exception as exc:
        logger.warning("crash_resume_hook_failed: err=%s", exc)

    # §17.135 — Embedder-identity drift detection. Must run AFTER the
    # migration runner (we need the cache_metadata table) but BEFORE any
    # path that exercises the embedder. Fail-soft: a DB hiccup logs but
    # does not crash startup; the drift just goes unnoticed until next
    # boot.
    try:
        from app.utils.embedder_drift import check_embedder_drift
        async with async_session() as _drift_db:  # §17.484 — module-level async_session (was a redundant local import)
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
    # §17.812 — init_clients() moved up (before crash-resume, audit C6); the
    # shared HTTP clients are already live by this point.

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
            # §17.164 — these used to be function-level imports here.
            # `import asyncio` inside lifespan() shadowed the module-level
            # binding at line 3, making `asyncio` local to the whole
            # function and turning the earlier reference at line 244
            # (Milvus connect) into an UnboundLocalError. That error was
            # caught + swallowed by the surrounding try/except, but it
            # also meant the Milvus connect handshake never completed —
            # downstream code then took the auto-create-empty-collection
            # path and orphaned the existing data on every restart.
            # asyncio / time / datetime are all imported at module level
            # (lines 3, 7, 9) so the function-local rebinds were redundant.
            from app.rerankers import _get_cross_encoder
            loop = asyncio.get_running_loop()
            _t0 = time.monotonic()
            await loop.run_in_executor(None, _get_cross_encoder)
            elapsed = round(time.monotonic() - _t0, 2)
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

    # §17.772 — when the MCP producer is enabled, the Streamable-HTTP session
    # manager backing the /mcp mount must run for the app's lifetime. Entered
    # here (not in a mounted sub-app's own lifespan, which Starlette does not
    # run for mounts) so tool calls have a live session manager.
    #
    # §17.808 — `StreamableHTTPSessionManager.run()` is once-per-instance, and
    # `mcp.session_manager` is a module-level singleton. In production the
    # lifespan runs exactly once, so this is fine. But `with TestClient(app)`
    # re-enters the lifespan on every test, and the 2nd entry's `run()` raises
    # `RuntimeError: … can only be called once per instance`, which propagated
    # out of `wait_startup` and ERRORed the setup of every subsequent web test
    # (the 99 errors + 2 failures in the §17.807 baseline). Drive the context
    # manually so a re-entry degrades gracefully — the mount just has no live
    # session manager for that entry, which no test exercises — instead of
    # taking the whole app startup down with it.
    _mcp_ctx = None
    if settings.mcp_server_enabled:
        from app.mcp_server import session_manager_context
        try:
            _mcp_ctx = session_manager_context()
            await _mcp_ctx.__aenter__()
            logger.info("mcp_server_mounted: /mcp streamable-http session manager running")
        except RuntimeError as exc:
            # Once-per-instance run() re-entered (only reachable via a repeated
            # lifespan, i.e. TestClient). Skip the live session manager.
            logger.warning('event="mcp_session_manager_reentry_skipped" error=%s', exc)
            _mcp_ctx = None
    yield

    # Shutdown
    if _mcp_ctx is not None:
        try:
            await _mcp_ctx.__aexit__(None, None, None)
        except Exception as exc:
            logger.warning('event="mcp_session_manager_exit_failed" error=%s', exc)
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
    # §17.855 (audit B7) — close the four Redis-backed caches' aioredis clients.
    # Each held a lazily-opened connection that shutdown never closed (harmless
    # on process exit, but a leak under test/reload and an unclean-close warning).
    for _closer_mod, _closer_fn in (
        ("app.utils.embedding_cache", "close_embedding_cache"),
        ("app.utils.fetch_cache", "close_fetch_cache"),
        ("app.utils.llm_response_cache", "close_llm_response_cache"),
        ("app.utils.rag_result_cache", "close_rag_result_cache"),
    ):
        try:
            _mod = __import__(_closer_mod, fromlist=[_closer_fn])
            await getattr(_mod, _closer_fn)()
        except Exception as exc:  # noqa: BLE001 — shutdown best-effort
            logger.warning('event="cache_close_failed" cache=%s error=%s', _closer_mod, exc)
    # MilvusClient.close() is sync; wrap on the same async-first principle
    # as the startup connect above (§17.591).
    try:
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, close_milvus_client)
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
    # §17.174 — bumped v1.1.0 → v1.2.0 to capture pre-existing OpenAPI
    # drift from §17.130 (POST /jobs/{job_id}/resume), §17.114/§17.121
    # (GET /research/verify/{session_id}), and the engineering-design
    # track §17.140-§17.155 (/design/*, /specs/*, /topology-selections/*,
    # /device-sizings/*/report, /digital-sizings/*/report). The §17.174
    # router refactor itself introduces no OpenAPI changes — paths,
    # function names, tags, response_models all preserved verbatim.
    version="1.4.0",
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
# §17.812 (audit C9) — outermost: refuse cross-origin state-changing requests to
# the auth-exempt /web UI (CSRF stopgap) before any handler or body processing.
# Passes non-/web and same-origin requests straight through, so SecurityHeaders
# (registered just before → second-outermost) still wraps every real response.
app.add_middleware(WebCsrfMiddleware)

# §17.438 — OTel FastAPI instrumentation MUST attach at app-build time, not in
# lifespan: it adds a middleware, and Starlette forbids that once the app has
# started ("Cannot add middleware after an application has started" — what
# aborted OTel init when §17.435's Phoenix backend was first activated). The
# TracerProvider + OTLP exporter + httpx/asyncpg wiring stay in lifespan's
# init_tracing(). No-op unless otel_enabled.
if settings.otel_enabled:
    try:
        from app.observability.otel import instrument_fastapi
        instrument_fastapi(app)
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning('event="otel_fastapi_instrument_failed" error=%s', exc)


# §17.441 — a pathologically deep JSON body (e.g. thousands of nested arrays)
# makes Starlette/Pydantic exceed Python's recursion limit while parsing. FastAPI
# converts JSONDecodeError → 422 but NOT RecursionError, so it fell through
# ErrorLoggingMiddleware's catch-all as a 500 — wrong status for malformed input,
# and worse, it wrote an "unrecoverable" error_logs row on every probe (tripping
# the unresolved-errors alert watchdog). Registering a handler here catches it in
# Starlette's inner ExceptionMiddleware, BEFORE ErrorLoggingMiddleware sees it, so
# no error_logs row is written. Surfaced by the §17.441 stress test (deeply-nested
# body 500'd on every JSON POST endpoint: /ideate, /research, /dag, /rag, /design).
@app.exception_handler(RecursionError)
async def _recursion_error_handler(request: Request, exc: RecursionError):
    logger.warning('event="recursion_error_rejected" path=%s', request.url.path)
    return JSONResponse(
        status_code=422,
        content={
            "error": "RecursionError",
            "message": "request body is too deeply nested",
            "path": request.url.path,
        },
    )

app.include_router(status_router)
app.include_router(assist_router)
app.include_router(observability_router)
app.include_router(alerts_router)
app.include_router(specs_router)
app.include_router(sizing_router)
app.include_router(report_router)
app.include_router(digital_report_router)
app.include_router(design_router)

# §17.174 — endpoint groups extracted from main.py into per-domain
# routers. Ordering preserved against pre-§17.174 declaration order so
# the OpenAPI snapshot (paths block sorted alphabetically by FastAPI)
# stays byte-identical; the include_router order only matters for
# routes with overlapping paths (none here).
from app.routers.workflow import router as workflow_router  # noqa: E402
from app.routers.research import router as research_router  # noqa: E402
from app.routers.jobs import router as jobs_router  # noqa: E402
from app.routers.schedule import router as schedule_router  # noqa: E402
from app.routers.gt import router as gt_router  # noqa: E402
from app.routers.prompts import router as prompts_router  # noqa: E402
from app.routers.rag import router as rag_router  # noqa: E402
from app.routers.nodes import router as nodes_router  # noqa: E402 — §17.478
from app.routers.artifacts import router as artifacts_router  # noqa: E402 — §17.565
from app.routers.route import router as route_router  # noqa: E402 — §17.628
from app.routers.mcp import router as mcp_router  # noqa: E402 — §17.772
from app.routers.model_proposals import router as model_proposals_router  # noqa: E402 — §17.803
from app.routers.models import router as models_router  # noqa: E402 — §17.813
from app.routers.auth_info import router as auth_info_router  # noqa: E402 — §17.815
from app.routers.operator_account import router as operator_account_router  # noqa: E402 — §17.840
from app.routers.meta import router as meta_router  # noqa: E402 — §17.817
from app.routers.profiles import router as profiles_router  # noqa: E402 — §17.809
app.include_router(workflow_router)
app.include_router(research_router)
app.include_router(jobs_router)
app.include_router(schedule_router)
app.include_router(gt_router)
app.include_router(prompts_router)
app.include_router(rag_router)
app.include_router(nodes_router)
app.include_router(artifacts_router)
app.include_router(route_router)
app.include_router(mcp_router)  # §17.772 — MCP server registry + introspection
app.include_router(model_proposals_router)  # §17.803 — role→model swap proposals
app.include_router(models_router)  # §17.813 — model-management JSON API (roles CRUD + probe)
app.include_router(auth_info_router)  # §17.815 — GET /auth/whoami (SPA login identity)
app.include_router(operator_account_router)  # §17.840 — admin account (password unlocks console)
app.include_router(meta_router)  # §17.817 — first-run state (connect-models wizard)
app.include_router(profiles_router)  # §17.809 — runtime compute profiles (/config/profile)


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

# Standalone operator SPA (no-build vanilla ES modules). Auth-bypassed at the
# browser layer (see app/auth.py _AUTH_EXEMPT_PREFIXES) so a browser can load
# the static assets keyless; the SPA itself carries X-API-Key (from
# localStorage) on every API call, so the orchestrator API stays gated. CSP
# still covers it via /ui in security_headers._HTML_PREFIXES. html=True serves
# index.html at /ui/ and falls back to it for client-routed paths.
app.mount("/ui", StaticFiles(directory="app/ui", html=True), name="ui")

# §17.772 — expose the engine as an MCP server over Streamable HTTP at /mcp,
# guarded by X-API-Key (a mounted sub-app bypasses the global auth dependency,
# so the guard reinstates it). Gated on mcp_server_enabled; the session manager
# is run in the lifespan above. The stdio transport (`python -m app.mcp_server`)
# is independent of this mount.
if settings.mcp_server_enabled:
    from app.mcp_server import ApiKeyASGIGuard, streamable_http_app  # noqa: E402
    app.mount(
        "/mcp",
        ApiKeyASGIGuard(
            streamable_http_app(),
            settings.scaffold_api_key.get_secret_value(),
        ),
    )

# §17.788 — native OpenAI-compatible surface at /v1 (POST /v1/chat/completions,
# GET /v1/models). Mounted as a sub-app so it bypasses the global X-API-Key
# dependency and carries its own Bearer-or-X-API-Key guard (require_openai_key).
# Gated on native_openai_enabled (default off) — the OWUI pipeline path is
# unchanged while off. See docs/native_openai_surface_plan.md.
if settings.native_openai_enabled:
    from app.routers.openai_compat import openai_app  # noqa: E402
    app.mount("/v1", openai_app)


@app.get("/", dependencies=[], include_in_schema=False)
async def web_root_redirect():
    """Redirect ``GET /`` to the standalone operator SPA at ``/ui/``.

    (Was ``/web/jobs`` before the SPA landed; the legacy HTMX UI still
    lives at ``/web/*``.) Excluded from OpenAPI
    (``include_in_schema=False``) because it's a convenience landing for
    browsers, not a stable API contract.
    """
    return RedirectResponse(url="/ui/", status_code=302)


# Note: request-id binding + X-Request-ID header are handled by RequestIdMiddleware
# (app/middleware/request_id.py); per-request access logging by PerformanceMiddleware
# (app/middleware/performance.py). A previous duplicate function-based middleware
# here generated a second UUID and emitted an access log without the request_id
# contextvar bound — removed.


# ── Health check (no auth — exempt from global require_api_key) ──────

@app.get("/health", dependencies=[], response_model=HealthCheckResponse)
async def health():
    return await build_health_response(app, _MIGRATION_STATE)


# §17.174 — /jobs/cleanup moved to routers/jobs.py.

_CONFIG_REDACT_KEYWORDS = ("key", "secret", "token", "password", "pass")
# URL-with-embedded-credentials: scheme://user:pass@host…
_CONFIG_URL_CREDS_RE = re.compile(r"^[a-z][a-z0-9+\-.]*://[^/@\s]+:[^/@\s]+@")


def _is_secret_field(name: str, value: object) -> bool:
    """True when a Settings field's value should be redacted in /config.

    Three triggers, in priority order:
      1. The field type is ``SecretStr``.
      2. The field NAME contains a sensitive keyword (``key`` / ``secret``
         / ``token`` / ``password`` / ``pass``) AND the value is a string.
      3. The field VALUE is a URL with embedded user:password credentials
         (e.g. ``postgresql+asyncpg://scaffold:abcd@host:5432/db``) —
         catches ``database_url`` and similar without false-positiving on
         credential-free URLs like ``http://172.18.0.1:11434``.
    We err on the side of over-redaction rather than leaking values
    via the public API.

    §17.611 (audit #4) — the keyword rule is gated on ``isinstance(value, str)``:
    a bare substring match previously redacted 10 non-secret INT fields (every
    ``*_max_tokens``, ``tool_call_coax_min_tokens``, ``fetch_cache_max_keys``) to
    ``(set)``, defeating /config's documented purpose (a dump safe to paste into
    bug reports) for zero security benefit. Ints carry no credential, so only
    string-valued keyword fields (github_token/huggingface_token) + SecretStr get
    redacted.
    """
    from pydantic import SecretStr
    if isinstance(value, SecretStr):
        return True
    lname = name.lower()
    if isinstance(value, str) and any(kw in lname for kw in _CONFIG_REDACT_KEYWORDS):
        return True
    if isinstance(value, str) and _CONFIG_URL_CREDS_RE.match(value):
        return True
    return False


# §17.854 (audit B4) — ops surface is admin-only under MULTI_USER_ENABLED: a
# scoped user key must not dump engine config or mutate the GLOBAL log level.
# Single-user, every key resolves to ADMIN_PRINCIPAL → zero behavior change.
@app.get("/config", tags=["ops"], response_model=ConfigResponse,
         dependencies=[Depends(require_admin)])
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
        # §17.603 — resolve default_factory fields (tool_call_coax_models,
        # alert_kind_cooldowns, node_escalation_order). finfo.default is
        # PydanticUndefined for those, so the reported default AND the
        # is_default comparison were wrong for every factory field (always
        # shown as overridden even on the built-in default).
        default = finfo.get_default(call_default_factory=True)
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


# ---------------------------------------------------------------------------
# §17.196 — Runtime log-level override (AUDIT 5.5)
# ---------------------------------------------------------------------------
#
# Pre-§17.196 the only way to bump verbosity was a restart with
# ``LOG_LEVEL=DEBUG`` — which dropped whatever in-flight debugging
# context the operator was working on. These three endpoints (GET to
# inspect, PATCH to override, POST to reset) let an authenticated
# operator change the root logger level live, without losing state.
# Auth-gated (inherited from the global require_api_key dependency).
# Audit trail: ``set_runtime_level`` emits a stable ``event="log_level_
# changed"`` line at WARNING so every change is grep-able in journald.

class _LogLevelPatchIn(BaseModel):
    """Request body for ``PATCH /config/log-level``.

    Accepted level names: ``DEBUG`` / ``INFO`` / ``WARNING`` / ``ERROR``
    / ``CRITICAL`` (case-insensitive). Unknown names return 400 — explicit
    operator action should fail loud (different from boot-time config
    which fails open to INFO).
    """
    level: str


@app.get("/config/log-level", tags=["ops"])
async def get_log_level():
    """Return the root logger's current level + the boot-time snapshot.

    Shape: ``{level, level_int, boot_level, boot_level_int, is_overridden}``.
    ``is_overridden`` is True when the current level differs from the boot
    snapshot — convenient for an operator dashboard that wants to flag
    "this orchestrator is running under a runtime override".
    """
    from app.logging_config import get_current_level
    return get_current_level()


@app.patch("/config/log-level", tags=["ops"],
           dependencies=[Depends(require_admin)])  # §17.854 (audit B4)
async def patch_log_level(body: _LogLevelPatchIn):
    """Override the root logger's level at runtime — survives until
    process restart OR ``POST /config/log-level/reset``.

    Idempotent: setting the level to its current value is a no-op
    (the audit-trail log line still fires so the no-op is observable).
    Unknown level names return 400.
    """
    from app.logging_config import set_runtime_level
    try:
        return set_runtime_level(body.level)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.post("/config/log-level/reset", tags=["ops"],
          dependencies=[Depends(require_admin)])  # §17.854 (audit B4)
async def reset_log_level():
    """Restore the root logger to the level the orchestrator booted with.

    Pair with ``PATCH /config/log-level`` — the audit's recommended flow:
    "I bumped to DEBUG for 5 minutes, now restore" without operator
    memory of what the boot value was.
    """
    from app.logging_config import reset_runtime_level
    return reset_runtime_level()





# §17.174 — endpoint extraction. The endpoint functions that used to
# live below (POST /ideas, POST /ideate, POST /ideate/confirm, GET /dag,
# POST /dag, POST /rag, GET /rag/dedup, POST/GET /gt*, GET/POST /prompts*,
# GET /exec/*, POST /optimize, POST /execute, POST /execute/all) have
# been moved into per-domain routers under app/routers/. main.py keeps
# only the lifespan + middleware + system endpoints (/health, /config,
# /metrics, /, /web). See app/routers/{workflow,research,jobs,
# schedule,gt,prompts,rag}.py for the moved endpoints. The OpenAPI
# snapshot is unchanged (function names, paths, tags, response_models
# all preserved verbatim across the move).


# All "Phase C management" endpoints moved to routers/jobs.py and
# routers/research.py per §17.174. See app.include_router(...) calls
# above.
