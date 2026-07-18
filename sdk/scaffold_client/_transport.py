"""Shared HTTP-error translation.

Both ``Client`` (sync httpx) and ``AsyncClient`` (async httpx) funnel their
responses through these helpers so the exception-mapping logic lives in
exactly one place. Network errors and non-2xx statuses both become
``ScaffoldError`` subclasses with messages that include the URL — callers
catching the base class still get an actionable message.
"""
from __future__ import annotations

from typing import Any

import httpx

from .errors import (
    AuthenticationError,
    ConflictError,
    ConnectionError,
    NotFoundError,
    OrchestratorError,
    PermissionError,
    RateLimitError,
    RequestError,
    TimeoutError,
)


def best_error_detail(resp: httpx.Response) -> str:
    """FastAPI emits ``{"detail": ...}`` on errors; pick that out when present.

    Falls back to the first 200 chars of the body, then to a status-only
    string. Never raises.
    """
    try:
        data = resp.json()
        if isinstance(data, dict) and "detail" in data:
            detail = data["detail"]
            return detail if isinstance(detail, str) else str(detail)
    except Exception:
        pass
    return resp.text[:200] if resp.text else f"HTTP {resp.status_code}"


def translate_request_error(exc: Exception, *, url: str) -> Exception:
    """Map an httpx network-layer exception to a ``ScaffoldError`` subclass."""
    if isinstance(exc, httpx.ConnectError):
        return ConnectionError(
            f"Cannot reach orchestrator at {url}. Is the container running?"
        )
    if isinstance(exc, httpx.TimeoutException):
        return TimeoutError(
            f"Request to {url} timed out. The orchestrator may be busy; retry, "
            "or check container logs."
        )
    if isinstance(exc, httpx.HTTPError):
        return ConnectionError(f"HTTP transport error talking to {url}: {exc}")
    return exc


def raise_for_status(resp: httpx.Response) -> None:
    """Raise the right ``ScaffoldError`` subclass for a non-2xx response.

    No-op for 2xx. 404 is included here — callers that want
    ``return None on 404`` semantics should catch ``NotFoundError`` and
    convert it themselves rather than hiding it in this function.
    """
    if resp.status_code < 400:
        return

    detail = best_error_detail(resp)
    code = resp.status_code

    if code == 401:
        raise AuthenticationError(
            f"API key rejected (401): {detail}. Set SCAFFOLD_API_KEY in the env."
        )
    if code == 403:
        raise PermissionError(f"Access forbidden (403): {detail}.")
    if code == 404:
        raise NotFoundError(f"Resource not found (404): {detail}.")
    if code == 409:
        raise ConflictError(f"State conflict (409): {detail}.")
    if code == 429:
        raise RateLimitError(f"Rate limited (429): {detail}.")
    if 400 <= code < 500:
        raise RequestError(f"Request rejected ({code}): {detail}.")
    raise OrchestratorError(
        f"Orchestrator error ({code}): {detail}. "
        "Check 'docker logs scaffold-orchestrator' for the stack trace."
    )


def parse_body(resp: httpx.Response) -> Any:
    """Return parsed JSON when the body is JSON; otherwise raw text.

    Endpoints occasionally return non-JSON (HTML error pages, plain-text
    health probes); falling back to text keeps the client robust without
    forcing every caller to handle parsing.
    """
    try:
        return resp.json()
    except ValueError:
        return resp.text
