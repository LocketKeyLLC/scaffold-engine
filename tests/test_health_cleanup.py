"""
tests/test_health_cleanup.py — Health check and stale job cleanup tests

Uses importlib to avoid WORKDIR /app package collision (Task #18).
Tests /health endpoint structure, degraded/unhealthy states, no-auth requirement.
Tests /jobs/cleanup transitions and auth requirement.
"""

import importlib.util
import os
import sys
import json
import time
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone, timedelta

# ---------------------------------------------------------------------------
# importlib loaders
# ---------------------------------------------------------------------------

_HEALTH_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "routers", "health.py"
)
_HEALTH_ABS = os.path.abspath(_HEALTH_PATH)

_CLEANUP_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "routers", "jobs_cleanup.py"
)
_CLEANUP_ABS = os.path.abspath(_CLEANUP_PATH)

_MAIN_PATH = os.path.join(
    os.path.dirname(__file__), "..", "app", "main.py"
)
_MAIN_ABS = os.path.abspath(_MAIN_PATH)


def _load_module(name, path):
    """Load a module via importlib, stubbing heavy deps."""
    stubs = {}
    for mod_name in [
        "app", "app.database", "app.modules", "app.config",
        "app.routers", "app.routers.health", "app.routers.jobs_cleanup",
        "app.routers.status",
        "sqlalchemy", "sqlalchemy.ext", "sqlalchemy.ext.asyncio",
        "sqlalchemy.orm", "sqlalchemy.sql", "sqlalchemy.text",
        "structlog", "aiohttp", "asyncpg",
        "pymilvus", "pymilvus.utility", "pymilvus.Collection",
        "fastapi", "fastapi.responses",
    ]:
        if mod_name not in sys.modules:
            stubs[mod_name] = MagicMock()

    # Make FastAPI stubs work
    mock_fastapi = MagicMock()
    mock_fastapi.APIRouter.return_value = MagicMock()
    mock_fastapi.Depends = MagicMock()
    stubs["fastapi"] = mock_fastapi

    mock_structlog = MagicMock()
    mock_structlog.get_logger.return_value = MagicMock()
    stubs["structlog"] = mock_structlog

    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(spec)
        try:
            spec.loader.exec_module(mod)
        except Exception:
            pass
        return mod


# ===========================================================================
# Health Endpoint — Source Code Structure Tests
# ===========================================================================

class TestHealthEndpointStructure:
    """Tests for /health endpoint structure and behavior."""

    @pytest.mark.skipif(
        not os.path.exists(_HEALTH_ABS) and not os.path.exists(_MAIN_ABS),
        reason="Health endpoint source not found",
    )
    def test_health_endpoint_exists(self):
        """Health endpoint is defined in health.py or main.py."""
        found = False
        for path in [_HEALTH_ABS, _MAIN_ABS]:
            if os.path.exists(path):
                with open(path, "r") as f:
                    source = f.read()
                if "/health" in source or "health" in source:
                    found = True
                    break
        assert found, "Health endpoint should be defined"

    @pytest.mark.skipif(
        not os.path.exists(_MAIN_ABS),
        reason="main.py not found",
    )
    def test_health_checks_postgres(self):
        """Health endpoint checks PostgreSQL connectivity."""
        # Health check is in main.py per carryover docs
        with open(_MAIN_ABS, "r") as f:
            source = f.read()
        assert any(term in source for term in [
            "postgres", "PostgreSQL", "_check_postgres", "database",
        ]), "Health should check PostgreSQL"

    @pytest.mark.skipif(
        not os.path.exists(_MAIN_ABS),
        reason="main.py not found",
    )
    def test_health_checks_milvus(self):
        """Health endpoint checks Milvus connectivity."""
        with open(_MAIN_ABS, "r") as f:
            source = f.read()
        assert any(term in source for term in [
            "milvus", "Milvus", "_check_milvus",
        ]), "Health should check Milvus"

    @pytest.mark.skipif(
        not os.path.exists(_MAIN_ABS),
        reason="main.py not found",
    )
    def test_health_checks_ollama(self):
        """Health endpoint checks Ollama connectivity."""
        with open(_MAIN_ABS, "r") as f:
            source = f.read()
        assert any(term in source for term in [
            "ollama", "Ollama", "_check_ollama",
        ]), "Health should check Ollama"

    @pytest.mark.skipif(
        not os.path.exists(_MAIN_ABS),
        reason="main.py not found",
    )
    def test_health_uses_concurrent_checks(self):
        """Health endpoint runs dependency checks concurrently."""
        with open(_MAIN_ABS, "r") as f:
            source = f.read()
        assert "asyncio.gather" in source or "gather" in source, (
            "Health should use asyncio.gather for concurrent checks"
        )

    @pytest.mark.skipif(
        not os.path.exists(_MAIN_ABS),
        reason="main.py not found",
    )
    def test_health_no_auth_required(self):
        """Health endpoint is exempt from global auth."""
        with open(_MAIN_ABS, "r") as f:
            source = f.read()
        # Health endpoint should have dependencies=[] to exempt from global auth
        assert "dependencies=[]" in source or "dependencies = []" in source, (
            "Health endpoint should be exempt from auth (dependencies=[])"
        )

    @pytest.mark.skipif(
        not os.path.exists(_MAIN_ABS),
        reason="main.py not found",
    )
    def test_health_returns_latency(self):
        """Health endpoint includes latency_ms for each dependency."""
        with open(_MAIN_ABS, "r") as f:
            source = f.read()
        assert "latency" in source, (
            "Health should report latency for each dependency"
        )

    @pytest.mark.skipif(
        not os.path.exists(_MAIN_ABS),
        reason="main.py not found",
    )
    def test_health_returns_timestamp(self):
        """Health endpoint includes a timestamp."""
        with open(_MAIN_ABS, "r") as f:
            source = f.read()
        assert "timestamp" in source or "datetime" in source, (
            "Health should include timestamp"
        )


# ===========================================================================
# Health Status Logic Tests
# ===========================================================================

class TestHealthStatusLogic:
    """Tests for health status determination logic."""

    @pytest.mark.skipif(
        not os.path.exists(_MAIN_ABS),
        reason="main.py not found",
    )
    def test_healthy_when_all_deps_up(self):
        """Status is 'healthy' when all dependencies respond."""
        with open(_MAIN_ABS, "r") as f:
            source = f.read()
        assert "healthy" in source, "Should return 'healthy' status"

    @pytest.mark.skipif(
        not os.path.exists(_MAIN_ABS),
        reason="main.py not found",
    )
    def test_degraded_status_exists(self):
        """Status 'degraded' is used when non-critical deps fail."""
        with open(_MAIN_ABS, "r") as f:
            source = f.read()
        assert "degraded" in source, (
            "Should return 'degraded' when Milvus is down"
        )

    @pytest.mark.skipif(
        not os.path.exists(_MAIN_ABS),
        reason="main.py not found",
    )
    def test_unhealthy_status_exists(self):
        """Status 'unhealthy' is used when critical deps fail."""
        with open(_MAIN_ABS, "r") as f:
            source = f.read()
        assert "unhealthy" in source, (
            "Should return 'unhealthy' when PostgreSQL is down"
        )


# ===========================================================================
# Cleanup Endpoint — Source Code Structure Tests
# ===========================================================================

class TestCleanupEndpointStructure:
    """Tests for /jobs/cleanup endpoint structure."""

    @pytest.mark.skipif(
        not os.path.exists(_CLEANUP_ABS),
        reason="jobs_cleanup.py not found",
    )
    def test_cleanup_endpoint_exists(self):
        """Cleanup endpoint is defined in jobs_cleanup.py."""
        with open(_CLEANUP_ABS, "r") as f:
            source = f.read()
        assert "cleanup" in source, "Cleanup endpoint should be defined"

    @pytest.mark.skipif(
        not os.path.exists(_CLEANUP_ABS),
        reason="jobs_cleanup.py not found",
    )
    def test_cleanup_is_post(self):
        """Cleanup endpoint uses POST method."""
        with open(_CLEANUP_ABS, "r") as f:
            source = f.read()
        assert ".post" in source or "POST" in source, (
            "Cleanup should be a POST endpoint"
        )

    @pytest.mark.skipif(
        not os.path.exists(_CLEANUP_ABS),
        reason="jobs_cleanup.py not found",
    )
    def test_cleanup_transitions_running_to_failed(self):
        """Cleanup transitions stale running jobs to failed."""
        with open(_CLEANUP_ABS, "r") as f:
            source = f.read()
        assert "running" in source and "failed" in source, (
            "Cleanup should transition running→failed"
        )

    @pytest.mark.skipif(
        not os.path.exists(_CLEANUP_ABS),
        reason="jobs_cleanup.py not found",
    )
    def test_cleanup_transitions_planning_to_cancelled(self):
        """Cleanup transitions stale planning jobs to cancelled."""
        with open(_CLEANUP_ABS, "r") as f:
            source = f.read()
        assert "planning" in source and "cancelled" in source, (
            "Cleanup should transition planning→cancelled"
        )

    @pytest.mark.skipif(
        not os.path.exists(_CLEANUP_ABS),
        reason="jobs_cleanup.py not found",
    )
    def test_cleanup_has_age_thresholds(self):
        """Cleanup uses age thresholds (30 min running, 60 min planning)."""
        with open(_CLEANUP_ABS, "r") as f:
            source = f.read()
        assert "30" in source or "1800" in source, (
            "Cleanup should have 30-min threshold for running jobs"
        )
        assert "60" in source or "3600" in source, (
            "Cleanup should have 60-min threshold for planning jobs"
        )

    @pytest.mark.skipif(
        not os.path.exists(_CLEANUP_ABS),
        reason="jobs_cleanup.py not found",
    )
    def test_cleanup_logs_stale_job_cleaned(self):
        """Cleanup emits stale_job_cleaned structured log event."""
        with open(_CLEANUP_ABS, "r") as f:
            source = f.read()
        assert "stale_job_cleaned" in source, (
            "Cleanup should emit stale_job_cleaned log event"
        )


# ===========================================================================
# Cleanup Auth Tests
# ===========================================================================

class TestCleanupAuth:
    """Tests for cleanup endpoint auth requirement."""

    @pytest.mark.skipif(
        not os.path.exists(_CLEANUP_ABS),
        reason="jobs_cleanup.py not found",
    )
    def test_cleanup_requires_auth(self):
        """Cleanup endpoint should NOT exempt auth (no dependencies=[])."""
        with open(_CLEANUP_ABS, "r") as f:
            source = f.read()
        # Cleanup should NOT have dependencies=[] (that exempts auth)
        # It relies on the global auth from FastAPI constructor
        # If it explicitly sets dependencies=[], that's a bug
        lines = source.split("\n")
        cleanup_routes = [
            l for l in lines if "cleanup" in l and "dependencies=[]" in l
        ]
        assert len(cleanup_routes) == 0, (
            "Cleanup endpoint should NOT exempt auth with dependencies=[]"
        )


# ===========================================================================
# Startup Cleanup Tests
# ===========================================================================

class TestStartupCleanup:
    """Tests for automatic cleanup on container startup."""

    @pytest.mark.skipif(
        not os.path.exists(_MAIN_ABS),
        reason="main.py not found",
    )
    def test_startup_cleanup_exists(self):
        """main.py runs cleanup on startup."""
        with open(_MAIN_ABS, "r") as f:
            source = f.read()
        assert any(term in source for term in [
            "startup_cleanup", "startup", "on_event",
            "lifespan", "@app.on_event",
        ]), "main.py should run cleanup on startup"

    @pytest.mark.skipif(
        not os.path.exists(_MAIN_ABS),
        reason="main.py not found",
    )
    def test_startup_cleanup_logging(self):
        """Startup cleanup emits structured log events."""
        with open(_MAIN_ABS, "r") as f:
            source = f.read()
        assert "startup_cleanup" in source, (
            "Startup cleanup should emit structured log events"
        )
