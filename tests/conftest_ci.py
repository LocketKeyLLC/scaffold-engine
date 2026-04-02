# ──────────────────────────────────────────────────────────
# tests/conftest_ci.py — Service availability fixtures
# ──────────────────────────────────────────────────────────
# Import this in your existing conftest.py or merge the contents.
#
# Usage in tests:
#   @pytest.mark.validate
#   def test_rag_query(requires_milvus):
#       ...
#
# In ci-smoke (cloud), Milvus/Postgres/Ollama are unavailable,
# so these fixtures auto-skip rather than fail with connection errors.

import os
import socket

import pytest


def _port_open(host: str, port: int, timeout: float = 2.0) -> bool:
    """Check if a TCP port is accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, ConnectionRefusedError, socket.timeout):
        return False


def _is_milvus_available() -> bool:
    """Milvus gRPC on default port."""
    return _port_open("localhost", 19530)


def _is_postgres_available() -> bool:
    return _port_open("localhost", 5432)


def _is_ollama_available() -> bool:
    return _port_open("localhost", 11434)


def _is_orchestrator_available() -> bool:
    return _port_open("localhost", 8000)


# ── Fixtures ──────────────────────────────────────────────

@pytest.fixture
def requires_milvus():
    if not _is_milvus_available():
        pytest.skip("Milvus not available (port 19530)")


@pytest.fixture
def requires_postgres():
    if not _is_postgres_available():
        pytest.skip("PostgreSQL not available (port 5432)")


@pytest.fixture
def requires_ollama():
    if not _is_ollama_available():
        pytest.skip("Ollama not available (port 11434)")


@pytest.fixture
def requires_orchestrator():
    if not _is_orchestrator_available():
        pytest.skip("Orchestrator not available (port 8000)")


@pytest.fixture
def requires_full_stack(requires_milvus, requires_postgres, requires_ollama):
    """Convenience: skip if ANY service in the full stack is down."""
    pass


# ── Marker helpers ────────────────────────────────────────
# Register in pyproject.toml:
#   [tool.pytest.ini_options]
#   markers = [
#       "smoke: unit tests, no live services",
#       "validate: integration tests, requires full stack",
#       "integration: alias for validate",
#   ]
