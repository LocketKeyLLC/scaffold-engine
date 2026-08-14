"""§17.772 — unit tests for the MCP producer's HTTP auth guard + helpers.

Not marked `smoke`: importing app.mcp_server constructs the MCPServer and imports
the mcp SDK at module load, which the no-services ci-smoke tier need not carry.
The tool handlers themselves are exercised by in-memory / stdio dogfood.
"""
from __future__ import annotations

import pytest

from app import mcp_server
from app.mcp_server import ApiKeyASGIGuard, _valid_uuid


class _StubApp:
    def __init__(self):
        self.called = False

    async def __call__(self, scope, receive, send):
        self.called = True


async def _drive(guard, scope):
    sent: list[dict] = []

    async def send(m):
        sent.append(m)

    async def receive():
        return {}

    await guard(scope, receive, send)
    return sent


def _http_scope(key=None):
    headers = []
    if key is not None:
        headers.append((b"x-api-key", key.encode()))
    return {"type": "http", "headers": headers}


@pytest.fixture(autouse=True)
def _auth_on(monkeypatch):
    # Deterministic: the guard must actually enforce regardless of the test env's
    # scaffold_auth_disabled default.
    monkeypatch.setattr(mcp_server.settings, "scaffold_auth_disabled", False)


class TestApiKeyGuard:
    async def test_valid_key_passes_through(self):
        app = _StubApp()
        sent = await _drive(ApiKeyASGIGuard(app, "secret"), _http_scope("secret"))
        assert app.called is True
        assert sent == []  # guard sent nothing; delegated

    async def test_wrong_key_401(self):
        app = _StubApp()
        sent = await _drive(ApiKeyASGIGuard(app, "secret"), _http_scope("nope"))
        assert app.called is False
        assert sent[0]["status"] == 401

    async def test_missing_key_401(self):
        app = _StubApp()
        sent = await _drive(ApiKeyASGIGuard(app, "secret"), _http_scope(None))
        assert app.called is False
        assert sent[0]["status"] == 401

    async def test_non_http_scope_passes(self):
        app = _StubApp()
        await _drive(ApiKeyASGIGuard(app, "secret"), {"type": "lifespan"})
        assert app.called is True

    async def test_auth_disabled_bypasses(self, monkeypatch):
        monkeypatch.setattr(mcp_server.settings, "scaffold_auth_disabled", True)
        app = _StubApp()
        await _drive(ApiKeyASGIGuard(app, "secret"), _http_scope(None))
        assert app.called is True


class TestValidUuid:
    def test_good_uuid(self):
        assert _valid_uuid("12345678-1234-5678-1234-567812345678") is not None

    def test_bad_uuid(self):
        assert _valid_uuid("not-a-uuid") is None

    def test_none(self):
        assert _valid_uuid(None) is None


def test_all_tools_registered():
    # The producer's advertised surface — guards against an accidental drop.
    # MCPServer keeps tools in an internal registry; assert via list_tools shape
    # is integration-y, so just confirm the functions exist as module attrs.
    for name in (
        "ideate", "run_job", "job_status", "job_results",
        "list_jobs", "rag_query", "research", "research_sessions",
    ):
        assert hasattr(mcp_server, name)
