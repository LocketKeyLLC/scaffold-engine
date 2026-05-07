"""Synchronous client for the Scaffold Engine orchestrator.

J.1.b ships the constructor + context-manager + a generic ``request()``
escape hatch. Typed wrapper methods (``health``, ``ideate``, ``confirm``,
``jobs.*`` …) land in J.1.c — they will reuse the same dispatch path.
"""
from __future__ import annotations

from typing import Any

import httpx

from . import _transport
from ._version import __version__


class Client:
    """Sync HTTP client for the Scaffold Engine API.

    Pre-injects ``X-API-Key`` when a key is configured. Network errors and
    non-2xx responses raise specific ``ScaffoldError`` subclasses; the
    base class catches them all.
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
        self._http = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Generic dispatch — caller-friendly form of ``httpx.Client.request``.

        Raises a ``ScaffoldError`` subclass on transport failure or
        non-2xx response. Returns parsed JSON on success.

        Typed wrapper methods (``health``, ``ideate``, …) added in J.1.c
        delegate here.
        """
        try:
            resp = self._http.request(method, path, params=params, json=json)
        except Exception as exc:
            raise _transport.translate_request_error(exc, url=self.base_url) from None
        _transport.raise_for_status(resp)
        return _transport.parse_body(resp)

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
