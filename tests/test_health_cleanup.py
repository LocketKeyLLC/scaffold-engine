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
      - Milvus: ``app.main.utility.list_collections`` + ``Collection``.
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
    mock_utility = MagicMock()
    mock_collection_cls = MagicMock()
    if milvus_up:
        mock_utility.list_collections.return_value = ["toon_v2"]
        mock_col_instance = MagicMock()
        mock_col_instance.num_entities = 8
        mock_collection_cls.return_value = mock_col_instance
    else:
        mock_utility.list_collections.side_effect = ConnectionError("Milvus down")

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
             patch("app.main.utility", mock_utility), \
             patch("app.main.Collection", mock_collection_cls), \
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

    mock_utility = MagicMock()
    mock_collection_cls = MagicMock()
    mock_utility.list_collections.return_value = ["toon_v2"]
    mock_col_instance = MagicMock()
    mock_col_instance.num_entities = 8
    mock_collection_cls.return_value = mock_col_instance

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
             patch("app.main.utility", mock_utility), \
             patch("app.main.Collection", mock_collection_cls), \
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
