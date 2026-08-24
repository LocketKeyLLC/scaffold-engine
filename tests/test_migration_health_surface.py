"""§17.812 (audit M3) — a startup migration failure is surfaced, not silent.

Before: run_migrations() returning {"status":"error"} produced a single log line
and the app booted on a PARTIAL schema. Now it records _MIGRATION_STATE, which
/health advertises as a warning, and (opt-in) fail_on_migration_error refuses to
serve.

These hit the live /health in the dev container (all services up → 200). The
migration warning is appended before the service-status warnings, so it is
present regardless of the ambient service health.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import app.main as main_mod  # alias BEFORE the `app` rebind below
from app.auth import require_api_key
from app.main import app  # FastAPI instance (shadows the `app` package name)


@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_api_key, None)


def test_fail_on_migration_error_defaults_false():
    """Code default is False — historical boot-on-partial-schema behavior is
    unchanged for existing installs + tests; fresh installs opt into hard-fail."""
    from app.config import settings

    assert settings.fail_on_migration_error is False


def test_health_surfaces_migration_failure(client, monkeypatch):
    monkeypatch.setattr(
        main_mod, "_MIGRATION_STATE",
        {"status": "error", "detail": "065_widget.sql: relation does not exist"},
    )
    body = client.get("/health").json()
    assert "warnings" in body
    assert any("startup migration FAILED" in w for w in body["warnings"])
    assert any("065_widget.sql" in w for w in body["warnings"])


def test_health_no_migration_warning_when_clean(client, monkeypatch):
    monkeypatch.setattr(main_mod, "_MIGRATION_STATE", None)
    body = client.get("/health").json()
    assert not any("startup migration FAILED" in w for w in body.get("warnings", []))
