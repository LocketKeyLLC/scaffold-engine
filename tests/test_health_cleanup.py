"""Behavioral tests for the /health endpoint.

Sprint X.17 — un-skipped + scoped down. Pre-X.17 the file was
module-level skipped because its TestReapStaleJobs class targeted the
old 4-statement / `rowcount`-based reaper shape — the live reaper now
runs 7 reapers and uses `len(fetchall())`, fully covered by
`tests/test_cleanup.py`. The reaper half is deleted; the /health half
(direct-call tests on `app.main.health`) is salvaged.

Distinct from `tests/test_x1_thresholds_and_health.py` — that file
covers X.1's reranker-prewarm check on `app.state`. This file covers
the broader status assembly (postgresql + ollama + milvus + redis +
embedding_cache + reranker → "healthy" / "degraded" / "unhealthy").

Run:  docker exec scaffold-orchestrator pytest tests/test_health_cleanup.py -m smoke --timeout=30 -v
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# /health — direct-call tests
# ---------------------------------------------------------------------------

def _call_health(pg_up=True, ollama_up=True, milvus_up=True):
    """Call ``app.main.health()`` with mocked backends; return the dict.

    Mock points (post-X.17):
      - PG: ``app.main.engine.connect()``
      - Ollama: ``app.utils.http_clients.get_ollama_client()`` returning
        a client whose ``.get(url, timeout=...)`` resolves to a fake
        ``httpx.Response`` (was ``app.main.httpx.AsyncClient`` pre-X.17 —
        Ollama check was migrated to the shared http-client registry).
      - Milvus: ``app.main.get_milvus_client`` (MilvusClient list_collections
        + get_collection_stats).
      - Redis: ``app.utils.embedding_cache.get_cache`` returning a cache
        whose ``stats`` is a dict and ``_get_redis()`` resolves to a
        connection with ``ping`` + ``dbsize``.
      - Reranker (X.1): ``app.state`` attributes ``reranker_prewarmed_at``
        / ``reranker_prewarm_elapsed_s`` etc. The check is invoked via
        ``getattr(app, "state", None)`` and tolerates missing attrs by
        returning ``status='skipped'``. We pass a SimpleNamespace with
        the prewarmed-at attribute set so the rendered status is "up".
    """

    # ── PG mock ────────────────────────────────────────────────────────────
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_connect_cm = AsyncMock()
    mock_connect_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_connect_cm.__aexit__ = AsyncMock(return_value=False)
    mock_engine = MagicMock()
    if pg_up:
        mock_engine.connect.return_value = mock_connect_cm
    else:
        mock_engine.connect.side_effect = ConnectionError("PG down")

    # ── Ollama mock (X.17: http_clients.get_ollama_client, not httpx.AsyncClient) ──
    mock_ollama_resp = MagicMock()
    mock_ollama_resp.raise_for_status = MagicMock()
    mock_ollama_resp.json.return_value = {"models": [{"name": "qwen3:4b"}]}
    mock_ollama_client = MagicMock()
    if ollama_up:
        mock_ollama_client.get = AsyncMock(return_value=mock_ollama_resp)
    else:
        mock_ollama_client.get = AsyncMock(
            side_effect=ConnectionError("Ollama down"),
        )

    # ── Milvus mock ────────────────────────────────────────────────────────
    # §17.591 — health now uses MilvusClient (get_milvus_client) instead of
    # the ORM utility/Collection: list_collections() + get_collection_stats().
    mock_milvus_client = MagicMock()
    if milvus_up:
        mock_milvus_client.list_collections.return_value = ["toon_v2"]
        mock_milvus_client.get_collection_stats.return_value = {"row_count": 8}
    else:
        mock_milvus_client.list_collections.side_effect = ConnectionError("Milvus down")

    # ── Redis / embedding-cache mock ───────────────────────────────────────
    mock_cache = MagicMock()
    mock_cache.stats = {"hits": 5, "misses": 2}
    mock_redis_conn = AsyncMock()
    mock_redis_conn.ping = AsyncMock()
    mock_redis_conn.dbsize = AsyncMock(return_value=10)
    mock_cache._get_redis = AsyncMock(return_value=mock_redis_conn)

    # ── Reranker (X.1) — pretend prewarm completed ─────────────────────────
    fake_state = SimpleNamespace(
        reranker_prewarmed_at="2026-05-08T00:00:00+00:00",
        reranker_prewarm_elapsed_s=12.5,
        reranker_prewarm_error=None,
        reranker_prewarm_skipped=False,
    )

    async def do_call():
        with patch("app.main.engine", mock_engine), \
             patch(
                 "app.utils.http_clients.get_ollama_client",
                 return_value=mock_ollama_client,
             ), \
             patch("app.main.get_milvus_client", return_value=mock_milvus_client), \
             patch(
                 "app.utils.embedding_cache.get_cache",
                 return_value=mock_cache,
             ):
            from app.main import app, health
            # Stash the test's reranker state on the live app object;
            # health() reads it via getattr(app, "state", None).
            old_state = getattr(app, "state", None)
            app.state = fake_state
            try:
                return await health()
            finally:
                if old_state is not None:
                    app.state = old_state
                else:
                    delattr(app, "state")

    return _run(do_call())


@pytest.mark.smoke
class TestHealthEndpointResponse:
    """``health()`` returns the expected envelope shape."""

    def test_health_returns_dict(self):
        result = _call_health()
        assert isinstance(result, dict)

    def test_health_has_status_field(self):
        result = _call_health()
        assert "status" in result
        assert result["status"] in ("healthy", "degraded", "unhealthy")

    def test_health_has_timestamp(self):
        result = _call_health()
        assert "timestamp" in result

    def test_health_has_checks_dict(self):
        result = _call_health()
        assert "checks" in result
        assert isinstance(result["checks"], dict)

    def test_healthy_when_all_up(self):
        result = _call_health()
        assert result["status"] == "healthy"

    def test_checks_include_pg_ollama_milvus(self):
        result = _call_health()
        checks = result["checks"]
        assert "postgresql" in checks
        assert "ollama" in checks
        assert "milvus" in checks

    def test_checks_include_redis_and_reranker(self):
        """X.1 + the redis-tuple migration extended the checks dict.
        Regression guard: both keys must be present in every response."""
        result = _call_health()
        checks = result["checks"]
        assert "redis" in checks
        assert "reranker" in checks

    def test_health_includes_auth_enabled_flag(self):
        """§17.96 — surface SCAFFOLD_AUTH_DISABLED posture on /health
        so `make doctor` can red-flag a no-auth deployment without
        grepping boot logs. Field is unauthenticated by design — it
        carries no secret, just the boolean."""
        result = _call_health()
        assert "auth_enabled" in result
        assert isinstance(result["auth_enabled"], bool)

    def test_health_auth_enabled_true_when_setting_false(self):
        """Inverse mapping: scaffold_auth_disabled=False (default)
        → auth_enabled=True. The /health surface flips the polarity
        because 'enabled' is the positive operator-facing concept."""
        from app.main import settings as _settings
        with patch.object(_settings, "scaffold_auth_disabled", False):
            result = _call_health()
        assert result["auth_enabled"] is True

    def test_health_auth_enabled_false_when_setting_true(self):
        """scaffold_auth_disabled=True → auth_enabled=False. This is
        what make doctor's RED check looks for."""
        from app.main import settings as _settings
        with patch.object(_settings, "scaffold_auth_disabled", True):
            result = _call_health()
        assert result["auth_enabled"] is False


def _call_health_with_sidecars(*, ngspice_up=True, verilator_up=True, symbiyosys_up=True):
    """§17.171 — variant of _call_health that lets each sim sidecar's HTTP
    client be mocked up/down independently. The other deps (PG/Ollama/
    Milvus/Redis) stay at their default _call_health=all-up shape, so this
    helper isolates the sidecar contract.

    The sim sidecar getters live in app.utils.http_clients; in test envs
    init_clients() has not been called so the production getters raise
    RuntimeError. _check_sidecar catches that and returns 'down', which
    is fine for the default-down tests below — but the up tests need an
    actual mock client whose .get('/health') resolves to a 200.
    """
    # Reuse _call_health's mocks for PG/Ollama/Milvus/Redis by calling
    # the same setup, then layer sidecar mocks on top before invoking
    # health(). Duplicates ~30 lines of mock setup; the alternative is a
    # *args/**kwargs spread on _call_health which makes the simpler
    # all-up tests harder to read. Verbosity here, simplicity above.
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_connect_cm = AsyncMock()
    mock_connect_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_connect_cm.__aexit__ = AsyncMock(return_value=False)
    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_connect_cm

    mock_ollama_resp = MagicMock()
    mock_ollama_resp.raise_for_status = MagicMock()
    mock_ollama_resp.json.return_value = {"models": [{"name": "qwen3:4b"}]}
    mock_ollama_client = MagicMock()
    mock_ollama_client.get = AsyncMock(return_value=mock_ollama_resp)

    # §17.591 — MilvusClient-based health (see get_milvus_client).
    mock_milvus_client = MagicMock()
    mock_milvus_client.list_collections.return_value = ["toon_v2"]
    mock_milvus_client.get_collection_stats.return_value = {"row_count": 8}

    mock_cache = MagicMock()
    mock_cache.stats = {"hits": 5, "misses": 2}
    mock_redis_conn = AsyncMock()
    mock_redis_conn.ping = AsyncMock()
    mock_redis_conn.dbsize = AsyncMock(return_value=10)
    mock_cache._get_redis = AsyncMock(return_value=mock_redis_conn)

    def _make_sidecar_client(up: bool):
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        client = MagicMock()
        if up:
            client.get = AsyncMock(return_value=mock_resp)
        else:
            client.get = AsyncMock(side_effect=ConnectionError("sidecar down"))
        return client

    fake_state = SimpleNamespace(
        reranker_prewarmed_at="2026-05-20T00:00:00+00:00",
        reranker_prewarm_elapsed_s=12.5,
        reranker_prewarm_error=None,
        reranker_prewarm_skipped=False,
    )

    async def do_call():
        with patch("app.main.engine", mock_engine), \
             patch("app.utils.http_clients.get_ollama_client", return_value=mock_ollama_client), \
             patch("app.main.get_milvus_client", return_value=mock_milvus_client), \
             patch("app.utils.embedding_cache.get_cache", return_value=mock_cache), \
             patch(
                 "app.utils.http_clients.get_ngspice_client",
                 return_value=_make_sidecar_client(ngspice_up),
             ), \
             patch(
                 "app.utils.http_clients.get_verilator_client",
                 return_value=_make_sidecar_client(verilator_up),
             ), \
             patch(
                 "app.utils.http_clients.get_symbiyosys_client",
                 return_value=_make_sidecar_client(symbiyosys_up),
             ):
            from app.main import app, health
            old_state = getattr(app, "state", None)
            app.state = fake_state
            try:
                return await health()
            finally:
                if old_state is not None:
                    app.state = old_state
                else:
                    delattr(app, "state")

    return _run(do_call())


@pytest.mark.smoke
class TestHealthSimSidecarChecks:
    """§17.171 — sim sidecars (ngspice/verilator/symbiyosys) on /health.

    Operator visibility for optional sidecar services. Their status does
    NOT affect the top-level `status` field — a wedged sidecar leaves
    /health 'healthy' so legacy/scaffold workflows keep working — but
    the per-sidecar state is surfaced in the checks dict so `make doctor`
    + dashboards can flag a down sidecar without the operator needing
    to probe :8001-8003 manually.
    """

    def test_checks_include_three_sidecars(self):
        result = _call_health()  # baseline: clients uninitialized → down
        checks = result["checks"]
        assert "ngspice" in checks
        assert "verilator" in checks
        assert "symbiyosys" in checks

    def test_each_sidecar_has_status_and_latency(self):
        result = _call_health()
        for name in ("ngspice", "verilator", "symbiyosys"):
            block = result["checks"][name]
            assert block["status"] in ("up", "down")
            assert isinstance(block.get("latency_ms"), int)

    def test_sidecar_down_does_not_degrade_overall_status(self):
        """Policy guard: a sidecar being 'down' must NOT flip /health
        away from 'healthy'. Sim is optional; legacy workflows must
        keep their green light. Failure mode this prevents: ngspice
        wedges → /health 'unhealthy' → docker healthcheck restarts
        the orchestrator → loops."""
        result = _call_health_with_sidecars(
            ngspice_up=False, verilator_up=False, symbiyosys_up=False,
        )
        assert result["status"] == "healthy"
        for name in ("ngspice", "verilator", "symbiyosys"):
            assert result["checks"][name]["status"] == "down"

    def test_all_sidecars_up_when_clients_return_200(self):
        result = _call_health_with_sidecars(
            ngspice_up=True, verilator_up=True, symbiyosys_up=True,
        )
        for name in ("ngspice", "verilator", "symbiyosys"):
            assert result["checks"][name]["status"] == "up"

    def test_mixed_sidecar_state(self):
        """One up, one down, one up — verify each sidecar's outcome is
        independent of the others. Catches a regression where the gather
        ordering swap accidentally mismatches a sidecar block with a
        different sidecar's result."""
        result = _call_health_with_sidecars(
            ngspice_up=True, verilator_up=False, symbiyosys_up=True,
        )
        assert result["checks"]["ngspice"]["status"] == "up"
        assert result["checks"]["verilator"]["status"] == "down"
        assert result["checks"]["symbiyosys"]["status"] == "up"


@pytest.mark.smoke
class TestHealthDegradedStates:
    """Status-derivation logic: PG + Ollama up + Milvus up = healthy;
    PG + Ollama up but Milvus down = degraded; either of PG/Ollama
    down = unhealthy. (Redis + reranker do NOT factor into the
    top-level status — they're informational; rationale: a Redis
    flip-flop shouldn't page operators on a still-functional stack.)"""

    def test_degraded_when_milvus_down(self):
        result = _call_health(milvus_up=False)
        assert result["status"] == "degraded"

    def test_unhealthy_when_pg_down(self):
        result = _call_health(pg_up=False)
        assert result["status"] == "unhealthy"

    def test_unhealthy_when_ollama_down(self):
        result = _call_health(ollama_up=False)
        assert result["status"] == "unhealthy"


# ---------------------------------------------------------------------------
# §17.194 — calibration block surfaced on /health
# ---------------------------------------------------------------------------

def _call_health_with_calibration(*, last_row=None, db_raises=False):
    """Variant of _call_health that lets the test stub the calibration probe.

    ``last_row`` is the (kind, created_at) tuple returned by the
    SELECT ... FROM system_alerts query. None = no rows = unknown status.
    ``db_raises`` simulates a DB-probe failure → falls into the unknown
    fail-safe branch.
    """
    from datetime import datetime, timezone
    from contextlib import asynccontextmanager

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_connect_cm = AsyncMock()
    mock_connect_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_connect_cm.__aexit__ = AsyncMock(return_value=False)
    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_connect_cm

    mock_ollama_resp = MagicMock()
    mock_ollama_resp.raise_for_status = MagicMock()
    mock_ollama_resp.json.return_value = {"models": [{"name": "qwen3:4b"}]}
    mock_ollama_client = MagicMock()
    mock_ollama_client.get = AsyncMock(return_value=mock_ollama_resp)

    # §17.591 — MilvusClient-based health (see get_milvus_client).
    mock_milvus_client = MagicMock()
    mock_milvus_client.list_collections.return_value = ["toon_v2"]
    mock_milvus_client.get_collection_stats.return_value = {"row_count": 8}

    mock_cache = MagicMock()
    mock_cache.stats = {"hits": 5, "misses": 2}
    mock_redis_conn = AsyncMock()
    mock_redis_conn.ping = AsyncMock()
    mock_redis_conn.dbsize = AsyncMock(return_value=10)
    mock_cache._get_redis = AsyncMock(return_value=mock_redis_conn)

    fake_state = SimpleNamespace(
        reranker_prewarmed_at="2026-05-08T00:00:00+00:00",
        reranker_prewarm_elapsed_s=12.5,
        reranker_prewarm_error=None,
        reranker_prewarm_skipped=False,
    )

    # Calibration probe: replace async_session with a CM whose db.execute
    # returns last_row from .first() or raises if db_raises.
    cal_db = AsyncMock()
    cal_result = MagicMock()
    cal_result.first.return_value = last_row
    if db_raises:
        cal_db.execute = AsyncMock(side_effect=RuntimeError("simulated DB down"))
    else:
        cal_db.execute = AsyncMock(return_value=cal_result)

    @asynccontextmanager
    async def fake_session():
        yield cal_db

    async def do_call():
        with patch("app.main.engine", mock_engine), \
             patch(
                 "app.utils.http_clients.get_ollama_client",
                 return_value=mock_ollama_client,
             ), \
             patch("app.main.get_milvus_client", return_value=mock_milvus_client), \
             patch(
                 "app.utils.embedding_cache.get_cache",
                 return_value=mock_cache,
             ), \
             patch("app.main.async_session", fake_session):
            from app.main import app, health
            old_state = getattr(app, "state", None)
            app.state = fake_state
            try:
                return await health()
            finally:
                if old_state is not None:
                    app.state = old_state
                else:
                    delattr(app, "state")

    return _run(do_call())


@pytest.mark.smoke
class TestHealthCalibrationBlock:
    """§17.194 — calibration: {status, last_check_at, last_kind} block on /health.

    Pre-§17.194 calibration cron health was visible only via the alerts
    table or by grepping journald. The block surfaces it for ``make
    doctor`` / dashboards / OWUI without an extra query.
    """

    def test_calibration_block_present_in_checks(self):
        result = _call_health_with_calibration(last_row=None)
        assert "calibration" in result["checks"]

    def test_unknown_when_no_calibration_alerts(self):
        result = _call_health_with_calibration(last_row=None)
        cal = result["checks"]["calibration"]
        assert cal["status"] == "unknown"
        assert cal["last_check_at"] is None
        assert cal["last_kind"] is None

    def test_ok_status_for_calibration_ok_kind(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 4, 1, 8, 5, tzinfo=timezone.utc)
        result = _call_health_with_calibration(last_row=("calibration.ok", ts))
        cal = result["checks"]["calibration"]
        assert cal["status"] == "ok"
        assert cal["last_kind"] == "calibration.ok"
        assert cal["last_check_at"] == ts.isoformat()

    def test_failed_status_for_calibration_failed_kind(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 4, 1, 8, 5, tzinfo=timezone.utc)
        result = _call_health_with_calibration(last_row=("calibration.failed", ts))
        cal = result["checks"]["calibration"]
        assert cal["status"] == "failed"
        assert cal["last_kind"] == "calibration.failed"

    def test_missed_status_for_calibration_no_fire_kind(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)
        result = _call_health_with_calibration(last_row=("calibration.no_fire", ts))
        cal = result["checks"]["calibration"]
        assert cal["status"] == "missed"
        assert cal["last_kind"] == "calibration.no_fire"

    def test_in_progress_status_for_calibration_started_kind(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc)
        result = _call_health_with_calibration(last_row=("calibration.started", ts))
        cal = result["checks"]["calibration"]
        assert cal["status"] == "in_progress"

    def test_unknown_when_db_probe_fails(self):
        """Fail-safe: a DB-probe exception must not break /health — falls
        through to status=unknown, no exception propagates."""
        result = _call_health_with_calibration(db_raises=True)
        cal = result["checks"]["calibration"]
        assert cal["status"] == "unknown"
        # /health itself still returns successfully (top-level shape OK).
        assert "checks" in result and "status" in result

    def test_calibration_missed_does_not_degrade_overall_status(self):
        """Policy guard: 'missed' is an operational concern, not a service
        degradation. Top-level /health must stay 'healthy' (mirroring
        §17.171's sim sidecar policy)."""
        from datetime import datetime, timezone
        result = _call_health_with_calibration(
            last_row=("calibration.no_fire",
                      datetime(2026, 4, 1, 9, 0, tzinfo=timezone.utc)),
        )
        assert result["status"] == "healthy"
        assert result["checks"]["calibration"]["status"] == "missed"

    def test_unknown_kind_falls_back_to_unknown_status(self):
        """A future calibration.* kind not in the mapping must not crash —
        falls through to status='unknown' with the raw kind exposed for
        operator triage."""
        from datetime import datetime, timezone
        result = _call_health_with_calibration(
            last_row=("calibration.future_event_we_havent_seen_yet",
                      datetime(2026, 4, 1, 8, 0, tzinfo=timezone.utc)),
        )
        cal = result["checks"]["calibration"]
        assert cal["status"] == "unknown"
        # The raw kind is still surfaced so the operator can grep journald.
        assert cal["last_kind"] == "calibration.future_event_we_havent_seen_yet"


# ---------------------------------------------------------------------------
# §17.386 — oom_alerts block surfaced on /health
# ---------------------------------------------------------------------------

def _call_health_with_oom(*, rows=None, host_rows=None, db_raises=False, window_hours=None):
    """Variant of _call_health that lets the test stub BOTH the calibration
    probe AND the §17.386 OOM-alerts rollup AND the §17.387 host-OOM rollup
    against the same async_session mock — /health calls async_session()
    THREE times (one per block) so the mock has to satisfy all three query
    shapes via SQL-text inspection.

    ``rows`` is a list of (container_name, count, most_recent_dt) tuples
    for the §17.386 container-OOM rollup. None → empty list.
    ``host_rows`` is the analog for the §17.387 host-OOM rollup —
    (comm, count, most_recent_dt) tuples. None → empty list.
    ``db_raises`` simulates a DB error → both rollups fail-open.
    ``window_hours`` lets a test override the settings default before the
    call; restored after.
    """
    from datetime import datetime, timezone
    from contextlib import asynccontextmanager

    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_connect_cm = AsyncMock()
    mock_connect_cm.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_connect_cm.__aexit__ = AsyncMock(return_value=False)
    mock_engine = MagicMock()
    mock_engine.connect.return_value = mock_connect_cm

    mock_ollama_resp = MagicMock()
    mock_ollama_resp.raise_for_status = MagicMock()
    mock_ollama_resp.json.return_value = {"models": [{"name": "qwen3:4b"}]}
    mock_ollama_client = MagicMock()
    mock_ollama_client.get = AsyncMock(return_value=mock_ollama_resp)

    # §17.591 — MilvusClient-based health (see get_milvus_client).
    mock_milvus_client = MagicMock()
    mock_milvus_client.list_collections.return_value = ["toon_v2"]
    mock_milvus_client.get_collection_stats.return_value = {"row_count": 8}

    mock_cache = MagicMock()
    mock_cache.stats = {"hits": 5, "misses": 2}
    mock_redis_conn = AsyncMock()
    mock_redis_conn.ping = AsyncMock()
    mock_redis_conn.dbsize = AsyncMock(return_value=10)
    mock_cache._get_redis = AsyncMock(return_value=mock_redis_conn)

    fake_state = SimpleNamespace(
        reranker_prewarmed_at="2026-05-08T00:00:00+00:00",
        reranker_prewarm_elapsed_s=12.5,
        reranker_prewarm_error=None,
        reranker_prewarm_skipped=False,
    )

    # Shared mock_db satisfies all three probes via SQL-text inspection
    # on execute(). Calibration probe → .first(). OOM probes →
    # .mappings().all() with different row shapes per kind.
    oom_mappings = [
        {"container": name, "count": count, "most_recent": mr}
        for (name, count, mr) in (rows or [])
    ]
    oom_result = MagicMock()
    oom_result.mappings.return_value.all.return_value = oom_mappings
    host_oom_mappings = [
        {"comm": comm, "count": count, "most_recent": mr}
        for (comm, count, mr) in (host_rows or [])
    ]
    host_oom_result = MagicMock()
    host_oom_result.mappings.return_value.all.return_value = host_oom_mappings
    cal_result = MagicMock()
    cal_result.first.return_value = None

    async def fake_execute(stmt, *args, **kwargs):
        if db_raises:
            raise RuntimeError("simulated DB down")
        sql = str(getattr(stmt, "text", stmt))
        if "container.oom_killed" in sql:
            return oom_result
        if "host.oom_killed" in sql:
            return host_oom_result
        return cal_result

    shared_db = AsyncMock()
    shared_db.execute = AsyncMock(side_effect=fake_execute)

    @asynccontextmanager
    async def fake_session():
        yield shared_db

    async def do_call():
        from app.config import settings as _settings
        old_window = _settings.oom_alerts_health_window_hours
        if window_hours is not None:
            _settings.oom_alerts_health_window_hours = window_hours
        try:
            with patch("app.main.engine", mock_engine), \
                 patch(
                     "app.utils.http_clients.get_ollama_client",
                     return_value=mock_ollama_client,
                 ), \
                 patch("app.main.get_milvus_client", return_value=mock_milvus_client), \
                 patch(
                     "app.utils.embedding_cache.get_cache",
                     return_value=mock_cache,
                 ), \
                 patch("app.main.async_session", fake_session):
                from app.main import app, health
                old_state = getattr(app, "state", None)
                app.state = fake_state
                try:
                    return await health()
                finally:
                    if old_state is not None:
                        app.state = old_state
                    else:
                        delattr(app, "state")
        finally:
            _settings.oom_alerts_health_window_hours = old_window

    return _run(do_call())


@pytest.mark.smoke
class TestHealthOomAlertsBlock:
    """§17.386 — oom_alerts: rollup of §17.161 system_alerts rows on /health.

    Pre-§17.386 OOM events landed in the system_alerts table (§17.161) but
    operators had to grep that table to know whether anything had been
    OOM-killed recently. This block surfaces a per-container count +
    most-recent timestamp over a configurable window, mirroring §17.194's
    calibration block in shape and fail-safe posture.
    """

    def test_oom_block_present_in_checks(self):
        result = _call_health_with_oom()
        assert "oom_alerts" in result["checks"]

    def test_empty_rollup_when_no_oom_rows(self):
        result = _call_health_with_oom(rows=[])
        oom = result["checks"]["oom_alerts"]
        assert oom["total"] == 0
        assert oom["most_recent_at"] is None
        assert oom["by_container"] == {}
        assert oom["window_hours"] >= 1

    def test_populated_rollup_with_per_container_counts(self):
        from datetime import datetime, timezone
        ts_orch = datetime(2026, 6, 2, 18, 30, tzinfo=timezone.utc)
        ts_milvus = datetime(2026, 6, 2, 17, 15, tzinfo=timezone.utc)
        result = _call_health_with_oom(rows=[
            ("scaffold-orchestrator", 3, ts_orch),
            ("milvus-standalone", 2, ts_milvus),
        ])
        oom = result["checks"]["oom_alerts"]
        assert oom["total"] == 5
        assert oom["by_container"] == {
            "scaffold-orchestrator": 3,
            "milvus-standalone": 2,
        }
        # most_recent_at must be the latest timestamp across containers,
        # not just the first row — explicit max() in the helper.
        assert oom["most_recent_at"] == ts_orch.isoformat()

    def test_disabled_when_window_zero(self):
        """window_hours=0 disables the /health surfacing entirely; alerts
        still land in system_alerts via §17.161 but aren't surfaced."""
        result = _call_health_with_oom(window_hours=0)
        oom = result["checks"]["oom_alerts"]
        assert oom == {"disabled": True, "window_hours": 0}

    def test_db_error_falls_back_to_empty_rollup(self):
        """Fail-safe: DB error → empty rollup, never breaks /health."""
        result = _call_health_with_oom(db_raises=True)
        oom = result["checks"]["oom_alerts"]
        assert oom["total"] == 0
        assert oom["by_container"] == {}
        # Top-level /health must still be intact.
        assert "checks" in result and "status" in result

    def test_oom_block_does_not_degrade_overall_status(self):
        """Policy guard: OOM events are post-hoc evidence, not a current
        service-degradation signal (containers are restarted by docker by
        the time /health reads). Top-level status must stay 'healthy'
        even with 100 OOMs in the window."""
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 2, 20, 0, tzinfo=timezone.utc)
        result = _call_health_with_oom(rows=[("scaffold-orchestrator", 100, ts)])
        assert result["status"] == "healthy"
        assert result["checks"]["oom_alerts"]["total"] == 100

    def test_null_container_name_renders_as_unknown(self):
        """Defensive: if a future malformed alert has no container_name in
        payload, the rollup must not crash. Bucket as '<unknown>' so the
        operator can see something landed but with an empty name."""
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 2, 19, 0, tzinfo=timezone.utc)
        result = _call_health_with_oom(rows=[(None, 1, ts)])
        oom = result["checks"]["oom_alerts"]
        assert oom["total"] == 1
        assert oom["by_container"] == {"<unknown>": 1}


@pytest.mark.smoke
class TestHealthHostOomAlertsBlock:
    """§17.387 — host_oom_alerts: rollup of host-scope OOM kills on /health.

    Parallel to TestHealthOomAlertsBlock (§17.386 container OOMs).
    The two blocks coexist with the same shape pattern (window_hours +
    total + most_recent_at + per-bucket dict) but different bucket keys
    (by_container vs by_comm) and different source alert kinds
    (container.oom_killed vs host.oom_killed).
    """

    def test_host_block_present_in_checks(self):
        result = _call_health_with_oom()
        assert "host_oom_alerts" in result["checks"]

    def test_empty_rollup_when_no_host_oom_rows(self):
        result = _call_health_with_oom(host_rows=[])
        host = result["checks"]["host_oom_alerts"]
        assert host["total"] == 0
        assert host["most_recent_at"] is None
        assert host["by_comm"] == {}
        assert host["window_hours"] >= 1

    def test_populated_rollup_with_per_comm_counts(self):
        from datetime import datetime, timezone
        ts_pg = datetime(2026, 6, 2, 18, 30, tzinfo=timezone.utc)
        ts_py = datetime(2026, 6, 2, 17, 15, tzinfo=timezone.utc)
        result = _call_health_with_oom(host_rows=[
            ("postgres", 2, ts_pg),
            ("python", 1, ts_py),
        ])
        host = result["checks"]["host_oom_alerts"]
        assert host["total"] == 3
        assert host["by_comm"] == {"postgres": 2, "python": 1}
        assert host["most_recent_at"] == ts_pg.isoformat()

    def test_disabled_when_window_zero(self):
        """The same `oom_alerts_health_window_hours=0` toggle that
        disables the §17.386 block also disables this one — symmetric
        operator control over both surfaces."""
        result = _call_health_with_oom(window_hours=0)
        host = result["checks"]["host_oom_alerts"]
        assert host == {"disabled": True, "window_hours": 0}

    def test_db_error_falls_back_to_empty_rollup(self):
        """Fail-safe: DB error → empty rollup, never breaks /health.
        Mirrors the §17.386 fail-open invariant."""
        result = _call_health_with_oom(db_raises=True)
        host = result["checks"]["host_oom_alerts"]
        assert host["total"] == 0
        assert host["by_comm"] == {}
        assert "checks" in result and "status" in result

    def test_host_block_does_not_degrade_overall_status(self):
        """Same policy as §17.386: post-hoc evidence, no current
        degradation. Even 100 host OOMs keep top-level status='healthy'."""
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 2, 20, 0, tzinfo=timezone.utc)
        result = _call_health_with_oom(host_rows=[("python", 100, ts)])
        assert result["status"] == "healthy"
        assert result["checks"]["host_oom_alerts"]["total"] == 100

    def test_container_and_host_blocks_are_independent(self):
        """The two rollups MUST stay independent — populating only one
        must leave the other empty (no cross-contamination via shared
        SQL routing). Critical because they target different operator
        actions (raise per-container cap vs. lower all caps / add RAM)."""
        from datetime import datetime, timezone
        ts = datetime(2026, 6, 2, 18, 0, tzinfo=timezone.utc)
        # Populate container only, assert host is empty.
        result = _call_health_with_oom(rows=[("scaffold-orchestrator", 5, ts)])
        assert result["checks"]["oom_alerts"]["total"] == 5
        assert result["checks"]["host_oom_alerts"]["total"] == 0
        # Populate host only, assert container is empty.
        result = _call_health_with_oom(host_rows=[("postgres", 3, ts)])
        assert result["checks"]["oom_alerts"]["total"] == 0
        assert result["checks"]["host_oom_alerts"]["total"] == 3


@pytest.mark.smoke
class TestHealthWarnings:
    """§17.446 (Phase B / B5) — advisory warnings[] that never change status."""

    def test_warnings_is_a_list(self):
        result = _call_health()
        assert isinstance(result.get("warnings"), list)

    def test_no_false_warning_for_up_subsystems(self):
        # pg/ollama/milvus/redis/reranker are all mocked up → none of them
        # should appear in warnings (sidecars may, since the unit env has none).
        result = _call_health()
        joined = " ".join(result["warnings"])
        assert "redis is down" not in joined
        assert "reranker is" not in joined

    def test_warnings_do_not_change_status(self):
        result = _call_health()
        # warnings can be non-empty (no sidecars in test env) yet status healthy.
        assert result["status"] == "healthy"
