"""§17.855 (audit B7) — /health probe logic, extracted from app/main.py.

The endpoint stays registered in main.py (``@app.get("/health")``); this module
holds the ~450-line probe body (``build_health_response``) plus the two advisory
helpers (``_check_reranker_state`` / ``_model_role_warnings``). main.py is the app
wiring, not a giant health function. Behavior-preserving move (§17.855); main.py
re-exports these three names so existing ``from app.main import ...`` imports and
tests keep working.
"""
import asyncio
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import text

from app.config import SWITCHABLE_ROLE_FIELDS, settings

logger = logging.getLogger("scaffold")


def _check_reranker_state(state) -> dict:
    """Sprint X.1: surface lifespan prewarm outcome to /health.

    Status:
      - "up"       — prewarm completed (state.reranker_prewarmed_at set)
      - "down"     — prewarm errored (state.reranker_prewarm_error set)
      - "skipped"  — SCAFFOLD_PREWARM_RERANKER=false at boot
      - "unknown"  — neither flag present (build pre-X.1, or app.state
                     not yet wired). Treated as non-fatal.

    §17.187: also includes ``model`` + ``score_range`` so an operator can
    see (a) which reranker is loaded and (b) what range its scores arrive
    on — "unknown (assumed [0,1])" flags an unregistered model that may
    silently make ``settings.confidence_threshold`` either trivially-met
    or never-met.

    Pulled out of health() to keep it directly unit-testable. ``state``
    is the FastAPI app's state object (or any object with the same
    attribute names).
    """
    # §17.187 — score-range surface, populated regardless of prewarm outcome
    # (an operator inspecting a "down" reranker still benefits from knowing
    # which model would have been loaded).
    from app.rerankers import get_score_range_info, reranker_load_failed
    model_name = getattr(settings, "model_reranker", None)
    score_range, _ = get_score_range_info(model_name)
    base = {"model": model_name, "score_range": score_range}

    # §17.812 (audit C4) — the LIVE loader state trumps the prewarm stamp. Prewarm
    # catches no exception when the load merely returns None, so it stamps
    # reranker_prewarmed_at and this would report "up" while the reranker is
    # actually dead and every query silently degraded to RRF-only. If the loader
    # has hard-failed, report down+degraded so /health.warnings fires.
    if reranker_load_failed():
        return {
            **base, "status": "down", "prewarmed": False, "degraded": True,
            "error": "CrossEncoder load failed — RAG on RRF-only fallback (auto-retries)",
        }

    if state is None:
        return {**base, "status": "unknown", "prewarmed": False}
    prewarmed_at = getattr(state, "reranker_prewarmed_at", None)
    elapsed = getattr(state, "reranker_prewarm_elapsed_s", None)
    error = getattr(state, "reranker_prewarm_error", None)
    skipped = getattr(state, "reranker_prewarm_skipped", False)
    if error:
        return {**base, "status": "down", "prewarmed": False, "error": error}
    if skipped:
        return {**base, "status": "skipped", "prewarmed": False}
    if prewarmed_at:
        return {
            **base,
            "status": "up", "prewarmed": True,
            "prewarmed_at": prewarmed_at, "elapsed_s": elapsed,
        }
    return {**base, "status": "unknown", "prewarmed": False}




def _model_role_warnings(pulled: set[str]) -> list[str]:
    """§17.819 (plan 6.2) — models unconfigured/unreachable advisory.

    The settings singleton IS the live effective config (env pins applied at
    boot, DB overrides replayed by load_overrides_into_settings and mutated
    in-place by set_override), so comparing it against the daemon's
    pulled-tag list (already fetched by _check_ollama — no extra call)
    catches a fresh install whose role tags aren't pulled before the first
    job fails on them. Advisory only; points at the connect-models wizard.
    """
    # §17.854 (audit B6) — normalize the ``:latest`` tag both ways, exactly like
    # validate_models does. Without this, MODEL_GENERAL=qwen2.5 with the daemon
    # listing ``qwen2.5:latest`` passed validate_models but /health still warned
    # "tags not pulled" and pointed the operator at the setup wizard — a lying
    # health signal.
    def _pulled(tag: str) -> bool:
        return (
            tag in pulled
            or f"{tag}:latest" in pulled
            or tag.removesuffix(":latest") in pulled
        )
    missing = sorted(
        f"{role}={getattr(settings, role)}"
        for role in SWITCHABLE_ROLE_FIELDS
        if not _pulled(getattr(settings, role))
    )
    if not missing:
        return []
    return [
        "model roles reference tags not pulled on the Ollama daemon: "
        + ", ".join(missing)
        + " — pull them (ollama pull <tag>) or open the connect-models "
        "wizard at /ui/#/setup"
    ]




async def build_health_response(app, migration_state) -> dict:
    """Build the /health response dict. ``app`` supplies ``app.state``
    (reranker prewarm); ``migration_state`` is main.py's startup-migration
    result (None when clean). Extracted from the old inline endpoint.

    §17.855 — engine / async_session / get_milvus_client are resolved from
    ``app.main`` at call time (not module-imported here) so the existing
    ``patch("app.main.engine")`` / ``async_session`` / ``get_milvus_client``
    test seams keep working after the move, and so the runtime import can't
    create a cycle (app.main imports THIS module at load)."""
    from app import main as _main
    engine = _main.engine
    async_session = _main.async_session
    get_milvus_client = _main.get_milvus_client
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
                client = get_milvus_client()
                if client is None:
                    return 0, 0
                colls = client.list_collections()
                entry_count = 0
                if "toon_v2" in colls:
                    stats = client.get_collection_stats("toon_v2")
                    entry_count = int(stats.get("row_count", 0))
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

    # §17.171 — sim sidecars. Optional surfaces (ngspice/verilator/symbiyosys);
    # absent or wedged sidecars do NOT degrade overall /health status — they
    # don't block legacy/scaffold workflows — but the operator gets
    # per-sidecar visibility in the checks dict. Each helper does a single
    # GET /health with a 5 s timeout via the shared httpx client pool.
    async def _check_sidecar(label: str):
        t0 = time.monotonic()
        try:
            from app.utils.http_clients import (
                get_ngspice_client, get_verilator_client, get_symbiyosys_client,
            )
            getters = {
                "ngspice": get_ngspice_client,
                "verilator": get_verilator_client,
                "symbiyosys": get_symbiyosys_client,
            }
            client = getters[label]()
            # Relative "/health" — base_url is the sidecar's URL per
            # app/utils/http_clients.py:_build_<name>(). 5 s read timeout
            # so a wedged sidecar can't stall /health past its own SLO.
            resp = await client.get("/health", timeout=5.0)
            resp.raise_for_status()
            return {"status": "up", "latency_ms": round((time.monotonic() - t0) * 1000)}
        except Exception:
            return {"status": "down", "latency_ms": round((time.monotonic() - t0) * 1000)}

    async def _check_ngspice():
        return await _check_sidecar("ngspice")

    async def _check_verilator():
        return await _check_sidecar("verilator")

    async def _check_symbiyosys():
        return await _check_sidecar("symbiyosys")

    # Each _check_* wraps its body in try/except Exception and returns a
    # dict on failure, so gather() cannot surface Exception objects from
    # these tasks; ``return_exceptions=True`` is left in only as
    # belt-and-suspenders for BaseException-derived cases (which we'd
    # actually want to propagate, not absorb).
    async def _check_calibration():
        """§17.194 — surface quarterly-calibration cron outcomes on /health.

        Pre-§17.194 calibration state was visible only via the
        ``system_alerts`` table (or via grepping journald for
        ``calibration.*`` events). The audit (AUDIT.md 3.6) flagged this
        as an observability gap — operators reading /health for "is
        anything broken" couldn't see that the quarterly drift-check
        hadn't run in 6 months.

        Status mapping:
          * ``ok``           — most recent ``calibration.*`` alert is
                                ``calibration.ok``
          * ``failed``       — most recent is ``calibration.failed``
          * ``missed``       — most recent is ``calibration.no_fire``
                                (watchdog detected a missed quarterly slot)
          * ``in_progress``  — most recent is ``calibration.started`` with
                                no follow-up yet
          * ``unknown``      — no calibration alerts on record, or DB probe
                                failed. Fail-safe: never crashes /health.
        """
        try:
            async with async_session() as db:
                # §17.611 (audit #40) — cap the query, not just the connect
                # handshake (connect_args timeout only covers connect). A locked
                # system_alerts table would otherwise block unauthenticated
                # /health indefinitely; the except below returns the fail-safe.
                row = await asyncio.wait_for(db.execute(
                    text(
                        "SELECT kind, created_at FROM system_alerts "
                        "WHERE kind LIKE 'calibration.%' "
                        "ORDER BY created_at DESC LIMIT 1"
                    ),
                ), timeout=2.0)
                rec = row.first()
            if rec is None:
                return {"status": "unknown", "last_check_at": None, "last_kind": None}
            kind = rec[0] or ""
            created = rec[1]
            kind_to_status = {
                "calibration.ok": "ok",
                "calibration.failed": "failed",
                "calibration.no_fire": "missed",
                "calibration.started": "in_progress",
            }
            status = kind_to_status.get(kind, "unknown")
            return {
                "status": status,
                "last_check_at": created.isoformat() if created else None,
                "last_kind": kind,
            }
        except Exception as exc:
            # Fail-safe: never break /health on calibration-probe issues.
            logger.debug("health_calibration_probe_failed: %s", exc)
            return {"status": "unknown", "last_check_at": None, "last_kind": None}

    async def _check_oom_alerts():
        """§17.386 — surface §17.161 OOM-kill alerts on /health.

        The §17.161 host-side oom_watcher writes one
        ``system_alerts`` row per Docker OOM event with
        kind='container.oom_killed' and payload.container_name. This
        block rolls them up into a per-container count + most-recent
        timestamp over the configured window so operators see "the
        signal" on /health without grepping system_alerts directly.

        Closes the §17.161 follow-up "oom-history surfacing on /health"
        explicitly logged as deferred at that commit. Mirrors §17.194's
        calibration block in shape (status dict in checks{}) and its
        fail-safe posture (every DB error → empty rollup, never
        crashes /health).

        Window of 0 disables the block (returns {"disabled": True})
        for operators who want the alerts persisted to system_alerts
        but not surfaced on the public /health.
        """
        window_h = settings.oom_alerts_health_window_hours
        if window_h == 0:
            return {"disabled": True, "window_hours": 0}
        try:
            async with async_session() as db:
                # §17.611 (audit #40) — per-query timeout (see _check_calibration).
                rows = await asyncio.wait_for(db.execute(
                    text(
                        "SELECT payload->>'container_name' AS container, "
                        "COUNT(*) AS count, MAX(created_at) AS most_recent "
                        "FROM system_alerts "
                        "WHERE kind = 'container.oom_killed' "
                        f"  AND created_at >= NOW() - INTERVAL '{int(window_h)} hours' "
                        "GROUP BY container "
                        "ORDER BY count DESC"
                    ),
                ), timeout=2.0)
                records = rows.mappings().all()
            by_container: dict[str, int] = {}
            most_recent: datetime | None = None
            for r in records:
                name = r["container"] or "<unknown>"
                by_container[name] = int(r["count"] or 0)
                mr = r["most_recent"]
                if mr is not None and (most_recent is None or mr > most_recent):
                    most_recent = mr
            return {
                "window_hours": window_h,
                "total": sum(by_container.values()),
                "most_recent_at": most_recent.isoformat() if most_recent else None,
                "by_container": by_container,
            }
        except Exception as exc:
            logger.debug("health_oom_alerts_probe_failed: %s", exc)
            return {
                "window_hours": window_h,
                "total": 0,
                "most_recent_at": None,
                "by_container": {},
            }

    async def _check_host_oom_alerts():
        """§17.387 — surface §17.387 host-scope OOM alerts on /health.

        The §17.387 host-side host_oom_watcher writes one
        ``system_alerts`` row per kernel host-OOM event (the kind that
        isn't tied to a docker container — global memory pressure on
        the host) with kind='host.oom_killed' and payload.comm. This
        block rolls them up into a per-comm count + most-recent
        timestamp over the configured window.

        Parallel to ``_check_oom_alerts`` (§17.386) — same shape, same
        fail-safe posture, same window setting. The two alert kinds are
        kept distinct because they correspond to different operator
        actions: container OOM → raise this container's mem_limit;
        host OOM → lower compose mem_limits or buy more RAM.
        """
        window_h = settings.oom_alerts_health_window_hours
        if window_h == 0:
            return {"disabled": True, "window_hours": 0}
        try:
            async with async_session() as db:
                # §17.611 (audit #40) — per-query timeout (see _check_calibration).
                rows = await asyncio.wait_for(db.execute(
                    text(
                        "SELECT payload->>'comm' AS comm, "
                        "COUNT(*) AS count, MAX(created_at) AS most_recent "
                        "FROM system_alerts "
                        "WHERE kind = 'host.oom_killed' "
                        f"  AND created_at >= NOW() - INTERVAL '{int(window_h)} hours' "
                        "GROUP BY comm "
                        "ORDER BY count DESC"
                    ),
                ), timeout=2.0)
                records = rows.mappings().all()
            by_comm: dict[str, int] = {}
            most_recent: datetime | None = None
            for r in records:
                comm = r["comm"] or "<unknown>"
                by_comm[comm] = int(r["count"] or 0)
                mr = r["most_recent"]
                if mr is not None and (most_recent is None or mr > most_recent):
                    most_recent = mr
            return {
                "window_hours": window_h,
                "total": sum(by_comm.values()),
                "most_recent_at": most_recent.isoformat() if most_recent else None,
                "by_comm": by_comm,
            }
        except Exception as exc:
            logger.debug("health_host_oom_alerts_probe_failed: %s", exc)
            return {
                "window_hours": window_h,
                "total": 0,
                "most_recent_at": None,
                "by_comm": {},
            }

    pg, ollama, milvus, redis_pair, ngspice, verilator, symbiyosys, calibration, oom_alerts, host_oom_alerts = await asyncio.gather(
        _check_pg(), _check_ollama(), _check_milvus(), _check_redis(),
        _check_ngspice(), _check_verilator(), _check_symbiyosys(),
        _check_calibration(), _check_oom_alerts(), _check_host_oom_alerts(),
        return_exceptions=True,
    )
    # §17.171 — defensive unpack. If _check_redis raises a BaseException
    # (CancelledError / KeyboardInterrupt), gather() returns the exception
    # object in place of the tuple — the pre-§17.171 code would then crash
    # on tuple-unpack and 500 the /health endpoint, defeating its purpose.
    if isinstance(redis_pair, BaseException):
        logger.warning("health_redis_check_raised_base_exception: %s", redis_pair)
        redis_info = {"status": "down", "keys": 0}
        cache_stats = verifier_cache_stats = rag_cache_stats = fetch_cache_stats = {}
    else:
        redis_info, cache_stats, verifier_cache_stats, rag_cache_stats, fetch_cache_stats = redis_pair
    # The three sidecars likewise return dicts; if gather absorbed a
    # BaseException, surface as 'down' rather than crashing the endpoint.
    for _name, _val in [("ngspice", ngspice), ("verilator", verilator), ("symbiyosys", symbiyosys)]:
        if isinstance(_val, BaseException):
            logger.warning("health_sidecar_check_raised: name=%s err=%s", _name, _val)
    if isinstance(ngspice, BaseException):
        ngspice = {"status": "down", "latency_ms": 0}
    if isinstance(verilator, BaseException):
        verilator = {"status": "down", "latency_ms": 0}
    if isinstance(symbiyosys, BaseException):
        symbiyosys = {"status": "down", "latency_ms": 0}
    # §17.194 — calibration probe; same fail-safe pattern as the sidecars.
    if isinstance(calibration, BaseException):
        logger.warning("health_calibration_check_raised: %s", calibration)
        calibration = {"status": "unknown", "last_check_at": None, "last_kind": None}
    # §17.386 — oom_alerts probe; same fail-safe pattern.
    if isinstance(oom_alerts, BaseException):
        logger.warning("health_oom_alerts_check_raised: %s", oom_alerts)
        oom_alerts = {
            "window_hours": settings.oom_alerts_health_window_hours,
            "total": 0,
            "most_recent_at": None,
            "by_container": {},
        }
    # §17.387 — host_oom_alerts probe; same fail-safe pattern.
    if isinstance(host_oom_alerts, BaseException):
        logger.warning("health_host_oom_alerts_check_raised: %s", host_oom_alerts)
        host_oom_alerts = {
            "window_hours": settings.oom_alerts_health_window_hours,
            "total": 0,
            "most_recent_at": None,
            "by_comm": {},
        }
    # §17.603 — pg/ollama/milvus were dereferenced (['status']) below WITHOUT
    # the BaseException guard redis/sidecars/probes get above. A per-task
    # BaseException from gather (e.g. a CancelledError) would TypeError-500 the
    # unauthenticated /health — the exact failure those guards were added to
    # prevent. Normalize the same way before building the checks dict.
    if isinstance(pg, BaseException):
        logger.warning("health_pg_check_raised: %s", pg)
        pg = {"status": "down", "latency_ms": 0}
    if isinstance(ollama, BaseException):
        logger.warning("health_ollama_check_raised: %s", ollama)
        ollama = {"status": "down", "latency_ms": 0}
    if isinstance(milvus, BaseException):
        logger.warning("health_milvus_check_raised: %s", milvus)
        milvus = {"status": "down", "latency_ms": 0}
    reranker = _check_reranker_state(getattr(app, "state", None))
    checks = {
        "postgresql": pg, "ollama": ollama, "milvus": milvus,
        "redis": redis_info, "embedding_cache": cache_stats,
        "verifier_cache": verifier_cache_stats,
        "rag_result_cache": rag_cache_stats,
        "fetch_cache": fetch_cache_stats,
        "reranker": reranker,
        # §17.171 — sim sidecars surfaced for operator visibility. Their
        # state does NOT affect the top-level `status` field below: a wedged
        # sidecar leaves /health "healthy" so legacy/scaffold workflows
        # keep working, but the per-sidecar 'down' is visible to anyone
        # who reads the checks dict (make doctor, dashboards, OWUI).
        "ngspice": ngspice,
        "verilator": verilator,
        "symbiyosys": symbiyosys,
        # §17.194 — quarterly calibration cron state. Read-only; advisory.
        # Does NOT affect top-level `status` (a missed calibration window
        # is an operational concern, not a service-degradation signal).
        "calibration": calibration,
        # §17.386 — OOM-event rollup over `oom_alerts_health_window_hours`.
        # Source: §17.161 host-side oom_watcher → system_alerts rows.
        # Read-only; advisory. Does NOT affect top-level `status` — an
        # OOM is post-hoc evidence of a mem_limit miss, not a current
        # service-degradation signal (the container has already been
        # restarted by docker by the time /health reads this).
        "oom_alerts": oom_alerts,
        # §17.387 — host-scope OOM-event rollup, parallel to oom_alerts.
        # Source: §17.387 host-side host_oom_watcher → system_alerts rows
        # with kind='host.oom_killed'. Same fail-safe posture; same
        # no-degradation policy. Distinct block because the operator
        # action differs (host OOM = host-wide pressure, container OOM
        # = per-container mem_limit miss).
        "host_oom_alerts": host_oom_alerts,
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

    # §17.446 (Phase B / B5) — advisory warnings. These do NOT change `status`
    # (the gate stays pg+ollama+milvus), but surface conditions an operator
    # reading only the top-level field would otherwise miss: a wedged cache,
    # a down sidecar, or recent OOM kills.
    warnings: list[str] = []
    if migration_state is not None:
        # §17.812 (audit M3) — a startup migration failed; the schema may be
        # partial and paths touching later tables will 500.
        warnings.append(
            f"startup migration FAILED — schema may be partial: "
            f"{migration_state.get('detail')}"
        )
    if redis_info.get("status") != "up":
        warnings.append("redis is down — caching and session memory are degraded")
    if reranker.get("status") not in ("up", "skipped"):
        warnings.append(f"reranker is {reranker.get('status')} — RAG falls back to RRF-only ranking")
    for _name in ("ngspice", "verilator", "symbiyosys"):
        if checks.get(_name, {}).get("status") not in ("up", None):
            warnings.append(f"{_name} sidecar is {checks[_name].get('status')} — EDA jobs will fail")
    if (oom_alerts.get("total") or 0) > 0:
        warnings.append(
            f"{oom_alerts['total']} container OOM event(s) in the last "
            f"{oom_alerts.get('window_hours')}h — check mem_limits"
        )
    if (host_oom_alerts.get("total") or 0) > 0:
        warnings.append(
            f"{host_oom_alerts['total']} host OOM event(s) in the last "
            f"{host_oom_alerts.get('window_hours')}h — host-wide memory pressure"
        )
    if ollama_up:
        warnings.extend(_model_role_warnings(set(ollama.get("models_loaded") or [])))

    return {
        "status": status,
        "warnings": warnings,
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
