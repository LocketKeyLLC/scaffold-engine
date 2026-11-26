"""§17.2037 (#182) — the /mcp mount answers on every lifespan entry.

`StreamableHTTPSessionManager.run()` is once-per-instance, and the engine kept
ONE manager for the process. §17.808 only stopped the second lifespan entry
from crashing startup: it skipped the manager, so /mcp had nothing running
behind it. Each lifespan entry now builds a fresh manager and the mount
forwards to the live one. Driven through the real app lifespan, twice.
"""
from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient

from app.config import settings

_INIT = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
               "clientInfo": {"name": "t", "version": "0"}},
}


def _initialize(client: TestClient) -> int:
    resp = client.post(
        "/mcp", json=_INIT,
        headers={"X-API-Key": settings.scaffold_api_key.get_secret_value(),
                 "Accept": "application/json, text/event-stream"},
    )
    return resp.status_code


@pytest.mark.skipif(not settings.mcp_server_enabled, reason="mount needs MCP_SERVER_ENABLED")
def test_mcp_answers_on_the_first_and_second_lifespan():
    from app.main import app
    with TestClient(app) as c:
        first = _initialize(c)
    with TestClient(app) as c:
        second = _initialize(c)
    assert (first, second) == (200, 200)


def test_each_context_runs_its_own_manager():
    """The module seam itself: two contexts in a row both enter, and each
    leaves a different manager live behind the mount."""
    from app import mcp_server

    async def go():
        seen = []
        for _ in range(2):
            async with mcp_server.session_manager_context():
                seen.append(mcp_server.live_session_manager())
        return seen

    a, b = asyncio.run(go())
    assert a is not None and b is not None and a is not b
