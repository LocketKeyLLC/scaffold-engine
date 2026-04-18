"""
tests/test_health_cleanup.py - Behavioral tests for /health and reap_stale_jobs

Tests health() by calling it directly with mocked backends.
Tests reap_stale_jobs() via mocked DB session.

Run:  docker exec scaffold-orchestrator pytest tests/test_health_cleanup.py -m smoke --timeout=30 -v
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from tests.conftest import make_mock_db


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ---------------------------------------------------------------------------
# /health tests - call the function directly
# ---------------------------------------------------------------------------

def _call_health(pg_up=True, ollama_up=True, milvus_up=True):
    """Call health() with mocked dependency checks, return response dict."""

    # Mock engine for PG check
    mock_conn = AsyncMock()
    mock_conn.execute = AsyncMock()
    mock_connect = AsyncMock()
    mock_connect.__aenter__ = AsyncMock(return_value=mock_conn)
    mock_connect.__aexit__ = AsyncMock(return_value=False)
    mock_engine = MagicMock()
    if pg_up:
        mock_engine.connect.return_value = mock_connect
    else:
        mock_engine.connect.side_effect = ConnectionError("PG down")

    # Mock httpx for Ollama check
    mock_http_resp = MagicMock()
    mock_http_resp.raise_for_status = MagicMock()
    mock_http_resp.json.return_value = {"models": [{"name": "qwen3:4b"}]}

    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)
    if ollama_up:
        mock_http_client.get = AsyncMock(return_value=mock_http_resp)
    else:
        mock_http_client.get = AsyncMock(side_effect=ConnectionError("Ollama down"))

    # Mock Milvus utility + Collection
    mock_utility = MagicMock()
    mock_collection_cls = MagicMock()
    if milvus_up:
        mock_utility.list_collections.return_value = ["toon_v2"]
        mock_col_instance = MagicMock()
        mock_col_instance.num_entities = 8
        mock_collection_cls.return_value = mock_col_instance
    else:
        mock_utility.list_collections.side_effect = ConnectionError("Milvus down")

    # Mock Redis/cache (inline import in health())
    mock_cache = MagicMock()
    mock_cache.stats = {"hits": 5, "misses": 2}
    mock_redis_conn = AsyncMock()
    mock_redis_conn.ping = AsyncMock()
    mock_redis_conn.dbsize = AsyncMock(return_value=10)
    mock_cache._get_redis = AsyncMock(return_value=mock_redis_conn)

    async def do_call():
        with patch("app.main.engine", mock_engine), \
             patch("app.main.httpx.AsyncClient", return_value=mock_http_client), \
             patch("app.main.utility", mock_utility), \
             patch("app.main.Collection", mock_collection_cls), \
             patch("app.utils.embedding_cache.get_cache", return_value=mock_cache):
            from app.main import health
            return await health()

    return _run(do_call())


@pytest.mark.smoke
class TestHealthEndpointResponse:
    """Test health() returns correct structure."""

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


@pytest.mark.smoke
class TestHealthDegradedStates:
    """Test health status logic for degraded/unhealthy."""

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
# reap_stale_jobs tests via mocked DB
# ---------------------------------------------------------------------------

def _make_reap_db(running_reaped=0, planning_reaped=0):
    """Mock DB for reap_stale_jobs."""
    r1 = MagicMock()
    r1.rowcount = running_reaped
    r2 = MagicMock()
    r2.rowcount = planning_reaped
    db = AsyncMock()
    r3 = MagicMock()
    r3.rowcount = 0
    r4 = MagicMock()
    r4.rowcount = 0
    db.execute = AsyncMock(side_effect=[r1, r2, r3, r4])
    db.commit = AsyncMock()
    return db


@pytest.mark.smoke
class TestReapStaleJobs:
    """Test reap_stale_jobs() returns correct counts."""

    def test_no_stale_jobs(self):
        db = _make_reap_db(running_reaped=0, planning_reaped=0)
        from app.modules.cleanup import reap_stale_jobs
        result = _run(reap_stale_jobs(db))
        assert result == {"running_to_failed": 0, "planning_to_cancelled": 0, "research_to_failed": 0, "paused_to_cancelled": 0}

    def test_running_jobs_reaped(self):
        db = _make_reap_db(running_reaped=3, planning_reaped=0)
        from app.modules.cleanup import reap_stale_jobs
        result = _run(reap_stale_jobs(db))
        assert result["running_to_failed"] == 3
        assert result["planning_to_cancelled"] == 0

    def test_planning_jobs_reaped(self):
        db = _make_reap_db(running_reaped=0, planning_reaped=2)
        from app.modules.cleanup import reap_stale_jobs
        result = _run(reap_stale_jobs(db))
        assert result["running_to_failed"] == 0
        assert result["planning_to_cancelled"] == 2

    def test_both_types_reaped(self):
        db = _make_reap_db(running_reaped=1, planning_reaped=4)
        from app.modules.cleanup import reap_stale_jobs
        result = _run(reap_stale_jobs(db))
        assert result["running_to_failed"] == 1
        assert result["planning_to_cancelled"] == 4

    def test_commits_after_reap(self):
        db = _make_reap_db(running_reaped=1, planning_reaped=0)
        from app.modules.cleanup import reap_stale_jobs
        _run(reap_stale_jobs(db))
        db.commit.assert_called_once()

    def test_returns_dict(self):
        db = _make_reap_db()
        from app.modules.cleanup import reap_stale_jobs
        result = _run(reap_stale_jobs(db))
        assert isinstance(result, dict)
        assert "running_to_failed" in result
        assert "planning_to_cancelled" in result
        assert "paused_to_cancelled" in result

    def test_expired_paused_sessions_reaped(self):
        """Separate fixture: returns a specific paused_to_cancelled count."""
        from unittest.mock import AsyncMock, MagicMock
        r1 = MagicMock(); r1.rowcount = 0
        r2 = MagicMock(); r2.rowcount = 0
        r3 = MagicMock(); r3.rowcount = 0
        r4 = MagicMock(); r4.rowcount = 2
        db = AsyncMock()
        db.execute = AsyncMock(side_effect=[r1, r2, r3, r4])
        db.commit = AsyncMock()
        from app.modules.cleanup import reap_stale_jobs
        result = _run(reap_stale_jobs(db))
        assert result["paused_to_cancelled"] == 2
        assert result["running_to_failed"] == 0
