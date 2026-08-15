"""In-process client for the engine's own HTTP endpoints (§17.790).

Native chat handlers reach the engine through an ``httpx.ASGITransport`` bound to
the FastAPI app rather than re-implementing each endpoint's query/render logic or
opening a real socket back to :8000. This reuses every existing route (jobs,
status, research, delete, …) with its real DB handling and response shape, runs
in-process on the same event loop (no deadlock — unlike a *sync* loopback, see the
``web_loopback_needs_sync_def`` gotcha), and re-runs auth with the engine's own
key so the surface stays gated.

The client is built lazily on first use — ``app.main`` is imported at call time,
never at module load, so there is no import cycle (this module is imported while
``app.main`` is still constructing the app).
"""
from __future__ import annotations

import logging
from typing import Any, AsyncIterator

import httpx

logger = logging.getLogger("scaffold.native_chat")

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        from app.config import settings
        from app.main import app as engine_app

        _client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=engine_app),
            base_url="http://engine",
            headers={"X-API-Key": settings.scaffold_api_key.get_secret_value()},
            timeout=httpx.Timeout(3600.0, connect=10.0),
        )
    return _client


async def get_json(path: str, *, params: dict[str, Any] | None = None) -> tuple[int, Any]:
    """GET ``path`` in-process. Returns ``(status_code, parsed_json_or_text)``."""
    resp = await _get_client().get(path, params=params)
    return resp.status_code, _body(resp)


async def request_json(
    method: str, path: str, *, json: Any | None = None, params: dict[str, Any] | None = None
) -> tuple[int, Any]:
    """Arbitrary in-process request. Returns ``(status_code, parsed_body)``."""
    resp = await _get_client().request(method, path, json=json, params=params)
    return resp.status_code, _body(resp)


async def stream_sse(
    path: str, *, json: Any | None = None
) -> AsyncIterator[tuple[str, Any]]:
    """POST ``path`` and yield ``(event, data)`` from its SSE stream.

    ``data`` is JSON-parsed when possible. Mirrors the engine's SSE wire format
    (``event: <name>\\n`` / ``data: <json>\\n\\n``); keepalive comment lines are
    skipped. Used to relay long-running endpoints (research, execute) as chat text.
    """
    import json as _json

    async with _get_client().stream("POST", path, json=json) as resp:
        if resp.status_code != 200:
            body = await resp.aread()
            yield "error", {"detail": f"HTTP {resp.status_code}: {body[:200].decode('utf-8', 'replace')}"}
            return
        event = "message"
        async for line in resp.aiter_lines():
            if not line:
                event = "message"
                continue
            if line.startswith(":"):
                continue  # keepalive comment
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                raw = line[len("data:"):].strip()
                try:
                    data = _json.loads(raw)
                except ValueError:
                    data = raw
                yield event, data


def _body(resp: httpx.Response) -> Any:
    ct = resp.headers.get("content-type", "")
    if "application/json" in ct:
        try:
            return resp.json()
        except ValueError:
            return resp.text
    return resp.text


async def aclose() -> None:
    """Close the shared client (test teardown / shutdown)."""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
