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
