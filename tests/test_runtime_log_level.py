"""§17.196 — runtime log-level override surface.

Covers the three new endpoints (GET / PATCH / POST reset) at
``/config/log-level`` and the helper functions in
``app/logging_config.py`` (``get_current_level`` / ``set_runtime_level``
/ ``reset_runtime_level``).

Each test resets the root logger's level + the boot-snapshot module-
level state via an autouse fixture so tests don't leak verbosity changes
into each other.
"""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app import logging_config as _lc
from app.auth import require_api_key


@pytest.fixture(autouse=True)
def _reset_log_state():
    """Snapshot the root logger level + boot snapshot before each test;
    restore after. Without this a test that bumps to DEBUG would leak
    into the next test's expected level."""
    root = logging.getLogger()
    pre_level = root.level
    pre_boot_int = _lc._BOOT_LEVEL_INT
    pre_boot_name = _lc._BOOT_LEVEL_NAME
    try:
        yield
    finally:
        root.setLevel(pre_level)
        _lc._BOOT_LEVEL_INT = pre_boot_int
        _lc._BOOT_LEVEL_NAME = pre_boot_name


@pytest.fixture
def client():
    from app.main import app
    app.dependency_overrides[require_api_key] = lambda: "test-key"
    yield TestClient(app)
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Helper tests — set_runtime_level / get_current_level / reset_runtime_level
# ---------------------------------------------------------------------------

class TestSetRuntimeLevel:
    def test_sets_root_logger_level_int(self):
        _lc.set_runtime_level("DEBUG")
        assert logging.getLogger().level == logging.DEBUG

    def test_accepts_case_insensitive_name(self):
        for name in ("debug", "Debug", "DEBUG"):
            logging.getLogger().setLevel(logging.INFO)
            _lc.set_runtime_level(name)
            assert logging.getLogger().level == logging.DEBUG

    def test_accepts_integer_level(self):
        _lc.set_runtime_level(logging.WARNING)
        assert logging.getLogger().level == logging.WARNING

    def test_unknown_name_raises_value_error(self):
        """Unknown name → ValueError (fail loud). Different from the
        boot-time _resolve_level which fails open to INFO — explicit
        operator action should surface the typo."""
        with pytest.raises(ValueError, match="unknown log level"):
            _lc.set_runtime_level("VERY_LOUD")

    def test_returns_current_state_dict(self):
        out = _lc.set_runtime_level("ERROR")
        assert out["level"] == "ERROR"
        assert out["level_int"] == logging.ERROR
        assert "boot_level" in out
        assert "is_overridden" in out

    def test_emits_structured_event_log_line(self, caplog):
        """Audit trail — every change emits event="log_level_changed" at
        WARNING with the from/to/reason fields. Stable grep token."""
        logging.getLogger().setLevel(logging.INFO)
        with caplog.at_level(logging.WARNING, logger="app.logging_config"):
            _lc.set_runtime_level("DEBUG")
        msgs = [r.message for r in caplog.records]
        match = [m for m in msgs if 'event="log_level_changed"' in m]
        assert match, f"expected event line; got: {msgs}"
        assert "from=INFO" in match[0]
        assert "to=DEBUG" in match[0]
        assert "reason=runtime_override" in match[0]

    def test_setting_same_level_still_emits_audit_line(self, caplog):
        """Idempotent in effect but observable: a no-op change still
        emits the audit line so an operator who hits PATCH twice in a
        row sees both attempts."""
        _lc.set_runtime_level("INFO")
        with caplog.at_level(logging.WARNING, logger="app.logging_config"):
            _lc.set_runtime_level("INFO")
        assert any(
            'event="log_level_changed"' in r.message and 'from=INFO' in r.message
            and 'to=INFO' in r.message
            for r in caplog.records
        )


class TestGetCurrentLevel:
    def test_returns_current_and_boot_snapshot(self):
        logging.getLogger().setLevel(logging.DEBUG)
        _lc._BOOT_LEVEL_INT = None  # force re-snapshot at DEBUG
        _lc._BOOT_LEVEL_NAME = None
        out = _lc.get_current_level()
        assert out["level"] == "DEBUG"
        assert out["boot_level"] == "DEBUG"
        assert out["is_overridden"] is False

    def test_is_overridden_flag_flips_after_set(self):
        logging.getLogger().setLevel(logging.INFO)
        _lc._BOOT_LEVEL_INT = None
        _lc._BOOT_LEVEL_NAME = None
        # First call snapshots INFO as boot.
        _lc.get_current_level()
        # Now override.
        _lc.set_runtime_level("DEBUG")
        out = _lc.get_current_level()
        assert out["is_overridden"] is True
        assert out["level"] == "DEBUG"
        assert out["boot_level"] == "INFO"


class TestResetRuntimeLevel:
    def test_restores_boot_level(self):
        logging.getLogger().setLevel(logging.WARNING)
        _lc._BOOT_LEVEL_INT = None
        _lc._BOOT_LEVEL_NAME = None
        # Snapshot WARNING as boot.
        _lc.get_current_level()
        # Override and reset.
        _lc.set_runtime_level("DEBUG")
        assert logging.getLogger().level == logging.DEBUG
        out = _lc.reset_runtime_level()
        assert logging.getLogger().level == logging.WARNING
        assert out["is_overridden"] is False

    def test_reset_emits_audit_event(self, caplog):
        """Reset goes through set_runtime_level so it gets the same audit line."""
        logging.getLogger().setLevel(logging.INFO)
        _lc._BOOT_LEVEL_INT = None
        _lc._BOOT_LEVEL_NAME = None
        _lc.get_current_level()  # snapshot INFO
        _lc.set_runtime_level("DEBUG")
        with caplog.at_level(logging.WARNING, logger="app.logging_config"):
            _lc.reset_runtime_level()
        assert any(
            'from=DEBUG' in r.message and 'to=INFO' in r.message
            for r in caplog.records
        )


# ---------------------------------------------------------------------------
# Endpoint tests — GET / PATCH / POST reset at /config/log-level
# ---------------------------------------------------------------------------

class TestLogLevelEndpoints:
    def test_get_returns_current_state(self, client):
        r = client.get("/config/log-level")
        assert r.status_code == 200
        body = r.json()
        assert "level" in body and "boot_level" in body
        assert body["level"] in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")

    def test_patch_sets_new_level(self, client):
        r = client.patch("/config/log-level", json={"level": "DEBUG"})
        assert r.status_code == 200
        body = r.json()
        assert body["level"] == "DEBUG"
        assert body["level_int"] == logging.DEBUG
        # Live verification — the root logger really did change.
        assert logging.getLogger().level == logging.DEBUG

    def test_patch_rejects_unknown_level(self, client):
        r = client.patch("/config/log-level", json={"level": "VERBOSE"})
        assert r.status_code == 400
        assert "unknown log level" in r.json()["detail"].lower()

    def test_patch_requires_level_field(self, client):
        """An empty body fails Pydantic validation → 422."""
        r = client.patch("/config/log-level", json={})
        assert r.status_code == 422

    def test_post_reset_restores_boot_level(self, client):
        # Snapshot the boot level.
        boot = client.get("/config/log-level").json()
        # Bump up.
        client.patch("/config/log-level", json={"level": "ERROR"})
        # Reset back.
        r = client.post("/config/log-level/reset")
        assert r.status_code == 200
        body = r.json()
        assert body["level"] == boot["boot_level"]
        assert body["is_overridden"] is False

    def test_patch_then_get_reflects_override(self, client):
        # Force a known boot snapshot at INFO so the post-PATCH WARNING is
        # demonstrably an override (rather than coincidentally matching
        # whatever level prior tests left the root logger at).
        logging.getLogger().setLevel(logging.INFO)
        _lc._BOOT_LEVEL_INT = None
        _lc._BOOT_LEVEL_NAME = None
        client.get("/config/log-level")  # snapshots INFO as boot
        client.patch("/config/log-level", json={"level": "WARNING"})
        r = client.get("/config/log-level")
        body = r.json()
        assert body["level"] == "WARNING"
        assert body["boot_level"] == "INFO"
        assert body["is_overridden"] is True

    def test_patch_idempotent_setting_same_level(self, client):
        client.patch("/config/log-level", json={"level": "INFO"})
        r = client.patch("/config/log-level", json={"level": "INFO"})
        assert r.status_code == 200
        assert r.json()["level"] == "INFO"

    def test_patch_case_insensitive(self, client):
        for variant in ("debug", "Debug", "DEBUG"):
            r = client.patch("/config/log-level", json={"level": variant})
            assert r.status_code == 200, f"failed for variant={variant!r}"
            assert r.json()["level"] == "DEBUG"
