"""Tests for app/middleware/error_logging.py — exception capture + classify.

Covers:
- Successful requests pass through unchanged
- Unhandled exceptions are caught and surfaced as structured 500 responses
- Exception classification (_classify_error) maps to the correct error_type
- Errors are persisted to the error_logs table
- A failure in error_logs persistence does NOT break the response (the
  middleware swallows the secondary failure so the user still gets the
  classified 500 instead of a bare ASGI crash)
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from app.middleware.error_logging import (
    ErrorLoggingMiddleware, _classify_error, _redact_secrets,
)


# ---------- _classify_error pure-function tests ----------

def test_classify_timeout_returns_timeout():
    assert _classify_error(httpx.TimeoutException("slow")) == "timeout"


def test_classify_connect_timeout_is_timeout_subclass():
    """ConnectTimeout is a TimeoutException subclass — should classify same."""
    assert _classify_error(httpx.ConnectTimeout("conn slow")) == "timeout"


def test_classify_http_error_returns_transient():
    """Generic HTTPError (non-timeout) → transient."""
    assert _classify_error(httpx.HTTPError("network blip")) == "transient"


@pytest.mark.parametrize("exc", [
    ValueError("bad input"),
    KeyError("missing"),
])
def test_classify_validation_family(exc):
    """ValueError + KeyError typically reflect bad user input."""
    assert _classify_error(exc) == "validation"


@pytest.mark.parametrize("exc", [
    TypeError("wrong type"),
    RuntimeError("???"),
])
def test_classify_unknown_returns_unrecoverable(exc):
    """TypeError joins the unrecoverable bucket — almost always programmer
    error (wrong arg count / type), not user-validation noise."""
    assert _classify_error(exc) == "unrecoverable"


# ---------- middleware integration tests ----------

@pytest.fixture
def app_with_endpoints():
    """App with a healthy endpoint and an exploding endpoint."""
    app = FastAPI()
    app.add_middleware(ErrorLoggingMiddleware)

    @app.get("/ok")
    async def ok():
        return {"status": "ok"}

    @app.get("/explode")
    async def explode():
        raise ValueError("kaboom")

    @app.get("/timeout")
    async def explode_timeout():
        raise httpx.TimeoutException("upstream slow")

    return app


@pytest.fixture
def mock_session():
    """Patch async_session so the middleware doesn't touch a real DB.
    Returns the AsyncMock for assertion access."""
    session = AsyncMock()
    session.execute = AsyncMock()
    session.commit = AsyncMock()

    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=session)
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "app.middleware.error_logging.async_session",
        return_value=cm,
    ):
        yield session


def test_passes_through_successful_response(app_with_endpoints, mock_session):
    client = TestClient(app_with_endpoints)
    r = client.get("/ok")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
    # No DB write on success.
    mock_session.execute.assert_not_awaited()


def test_catches_exception_returns_structured_500(app_with_endpoints, mock_session):
    client = TestClient(app_with_endpoints, raise_server_exceptions=False)
    r = client.get("/explode")
    assert r.status_code == 500
    body = r.json()
    assert body["error"] == "ValueError"
    assert "kaboom" in body["message"]
    assert body["path"] == "/explode"


def test_persists_error_to_error_logs(app_with_endpoints, mock_session):
    """The middleware must call session.execute exactly once with bind
    params containing the classified error_type."""
    client = TestClient(app_with_endpoints, raise_server_exceptions=False)
    client.get("/explode")  # ValueError → "validation"

    mock_session.execute.assert_awaited_once()
    bind = mock_session.execute.await_args.args[1]
    assert bind["error_type"] == "validation"
    assert "kaboom" in bind["error_message"]
    assert bind["stack_trace"]  # non-empty


def test_timeout_classification_threaded_through(app_with_endpoints, mock_session):
    client = TestClient(app_with_endpoints, raise_server_exceptions=False)
    client.get("/timeout")
    bind = mock_session.execute.await_args.args[1]
    assert bind["error_type"] == "timeout"


def test_persistence_failure_does_not_break_response(app_with_endpoints):
    """If error_logs persistence raises, the user MUST still get a 500
    instead of a bare ASGI crash. Regression guard for the case where
    DB is down at the same moment an endpoint fails."""
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(side_effect=RuntimeError("db is down"))
    cm.__aexit__ = AsyncMock(return_value=None)

    with patch(
        "app.middleware.error_logging.async_session",
        return_value=cm,
    ):
        client = TestClient(app_with_endpoints, raise_server_exceptions=False)
        r = client.get("/explode")

    assert r.status_code == 500
    assert r.json()["error"] == "ValueError"


# ---------- _redact_secrets unit tests (§17.162) ----------

class TestRedactSecrets:
    def test_empty_input_passes_through(self):
        assert _redact_secrets("") == ""
        assert _redact_secrets(None) is None  # noqa: type-check intentional

    def test_no_secrets_pass_through_unchanged(self):
        msg = "ValueError: expected int, got str"
        assert _redact_secrets(msg) == msg

    def test_redacts_openai_style_sk_key(self):
        out = _redact_secrets("error: sk-abc123def456ghi789jklmnop failed")
        assert "sk-abc123def456ghi789jklmnop" not in out
        assert "[REDACTED]" in out

    def test_short_sk_below_threshold_not_redacted(self):
        # 16-char tail minimum — shorter strings like "sk-short" are
        # unlikely to be real keys, don't false-positive on test data.
        out = _redact_secrets("sk-short")
        assert out == "sk-short"

    def test_redacts_bearer_token(self):
        out = _redact_secrets("Authorization: Bearer eyJhbGc.foo.bar")
        assert "eyJhbGc.foo.bar" not in out
        assert "[REDACTED]" in out

    def test_redacts_basic_auth_token(self):
        out = _redact_secrets("Authorization: Basic dXNlcjpwYXNz")
        assert "dXNlcjpwYXNz" not in out

    def test_bearer_is_case_insensitive(self):
        for prefix in ("Bearer", "bearer", "BEARER"):
            out = _redact_secrets(f"{prefix} abc123def456")
            assert "abc123def456" not in out

    def test_redacts_url_embedded_credentials(self):
        out = _redact_secrets("connecting to https://user:hunter2@db.example.com/foo failed")
        assert "user:hunter2" not in out
        assert "://[REDACTED]@db.example.com" in out

    def test_redacts_json_api_key_field(self):
        out = _redact_secrets('payload was {"api_key": "sk-secret-123-pasted-in-body"}')
        assert "sk-secret-123-pasted-in-body" not in out
        assert "api_key" in out  # key name preserved for debuggability
        assert "[REDACTED]" in out

    def test_redacts_form_encoded_api_key(self):
        out = _redact_secrets("api_key=hunter2 in body")
        assert "hunter2" not in out
        assert "api_key=" in out

    def test_redacts_password_token_secret_keys(self):
        for key in ("password", "secret", "token", "authorization"):
            out = _redact_secrets(f'"{key}": "leaked-value-here"')
            assert "leaked-value-here" not in out, f"failed for key={key}"

    def test_does_not_redact_token_count_field(self):
        # tokens_prompt / tokens_completion are token COUNTS (telemetry),
        # not credentials. The regex requires the key to be followed by
        # `:` or `=` with the literal name `token`, so `tokens_prompt: 100`
        # has the `s_prompt` between `token` and `:` — no match.
        msg = '{"tokens_prompt": 100, "tokens_completion": 50}'
        assert _redact_secrets(msg) == msg

    def test_does_not_false_positive_on_key_error_message(self):
        # KeyError messages mention key names but don't have key=value form.
        msg = "KeyError: 'api_key' missing from request"
        assert _redact_secrets(msg) == msg

    def test_handles_multiple_secrets_in_one_message(self):
        msg = ("upstream error: Bearer eyJabc.def.ghi at "
               "https://user:pw@svc:8080/x with sk-AAAAAAAAAAAAAAAAAAAA")
        out = _redact_secrets(msg)
        assert "eyJabc.def.ghi" not in out
        assert "user:pw" not in out
        assert "sk-AAAAAAAAAAAAAAAAAAAA" not in out


# ---------- wire-vs-DB redaction parity (§17.162) ----------

def test_wire_500_redacts_secret_but_db_keeps_raw(mock_session):
    """The wire echo gets sanitized while the DB record keeps the raw text
    for operator debugging via /observability/errors."""
    leaked = "sk-AAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
    app = FastAPI()
    app.add_middleware(ErrorLoggingMiddleware)

    @app.get("/leak")
    async def leak():
        raise ValueError(f"upstream rejected key {leaked}")

    client = TestClient(app, raise_server_exceptions=False)
    r = client.get("/leak")

    assert r.status_code == 500
    wire_msg = r.json()["message"]
    assert leaked not in wire_msg, "wire response must not echo the secret"
    assert "[REDACTED]" in wire_msg

    # DB binding for the INSERT — error_message should be RAW
    mock_session.execute.assert_awaited_once()
    bind = mock_session.execute.await_args.args[1]
    assert leaked in bind["error_message"], "DB record should retain raw text for operator debug"
