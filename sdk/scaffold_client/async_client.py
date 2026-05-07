"""Asynchronous client for the Scaffold Engine orchestrator.

J.1.b ships the constructor + async-context-manager + a generic
``request()`` escape hatch. Typed wrapper methods + SSE streaming
helpers (``aiter_research``, ``aiter_execute_all``) land in J.1.d.
"""
from __future__ import annotations

from typing import Any

import httpx

from . import _transport
from ._version import __version__


class AsyncClient:
    """Async HTTP client for the Scaffold Engine API.

    Mirrors ``Client`` but with awaitable methods. Use as an
    ``async with`` context manager so the underlying httpx pool is
    closed deterministically.
    """

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        headers: dict[str, str] = {"User-Agent": f"scaffold-client/{__version__}"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Generic dispatch. Raises ``ScaffoldError`` subclass on failure."""
        try:
            resp = await self._http.request(method, path, params=params, json=json)
        except Exception as exc:
            raise _transport.translate_request_error(exc, url=self.base_url) from None
        _transport.raise_for_status(resp)
        return _transport.parse_body(resp)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()
