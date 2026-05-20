"""Scaffold Engine — Error logging middleware.

Catches unhandled exceptions from any endpoint, logs to error_logs table,
and returns a structured error response.

Step 8 of 23-step build plan.
"""

from __future__ import annotations

import logging
import re
import traceback
from urllib.parse import urlparse

import httpx

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse
from sqlalchemy import text

try:
    from pymilvus.exceptions import MilvusException
except ImportError:  # pragma: no cover — pymilvus is required, but keep the
    # import defensive so a stripped install can still load the middleware.
    class MilvusException(Exception):  # type: ignore[no-redef]
        pass

from app.config import settings
from app.database import async_session

logger = logging.getLogger("scaffold.errors")


# §17.162 — secret-shape redaction for wire 500 + log emission.
# FastAPI's built-in handlers catch ValidationError and HTTPException
# before this middleware sees them, so the residual exception surface
# is programming bugs + httpx/asyncpg transport errors. Those rarely
# contain credential VALUES — but defense-in-depth says we should not
# blindly echo str(exc) to either the wire or the log. The DB record
# stays raw because /observability/errors is auth-gated.
_REDACT_SK = re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b")
_REDACT_BEARER = re.compile(r"\b(?:Bearer|Basic)\s+[A-Za-z0-9._\-=]+", re.IGNORECASE)
_REDACT_URL_CREDS = re.compile(r"://[^:@/\s]+:[^@/\s]+@")
# Match secret-shaped key=value pairs in JSON / dict / query-string form.
# group(1) is the key + separator (preserved); group(2) is the value (redacted).
_REDACT_KV = re.compile(
    r"(?i)(['\"]?(?:api[_-]?key|password|secret|token|auth(?:orization)?)['\"]?\s*[:=]\s*)"
    r"['\"]?([^'\",\s}\]]+)",
)


def _redact_secrets(text_in: str) -> str:
    """Scrub common credential-shaped patterns from a string.

    Applied to wire 500 responses and to log lines. The DB record keeps
    the raw exception text — error_logs is operator-gated via
    /observability/errors, so the raw signal stays available for
    debugging while the broader-surface wire echo gets redacted.
    """
    if not text_in:
        return text_in
    out = _REDACT_SK.sub("[REDACTED]", text_in)
    out = _REDACT_BEARER.sub("[REDACTED]", out)
    out = _REDACT_URL_CREDS.sub("://[REDACTED]@", out)
    out = _REDACT_KV.sub(lambda m: m.group(1) + "[REDACTED]", out)
    return out


class ErrorLoggingMiddleware(BaseHTTPMiddleware):
    """Intercept unhandled exceptions → write to error_logs → return 500/503."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            tb = traceback.format_exc()
            error_msg_raw = str(exc)[:1000]
            error_msg_safe = _redact_secrets(error_msg_raw)
            logger.error(
                "http_request_failed: exception=%s method=%s path=%s error=%s",
                type(exc).__name__, request.method, request.url.path, error_msg_safe,
            )

            # Classify error type
            error_type = _classify_error(exc)

            # Persist to error_logs (fire-and-forget, don't let logging fail
            # the response). DB record keeps the raw message + raw traceback —
            # /observability/errors is auth-gated, so operator debugging
            # retains full fidelity while wire + journald see redacted form.
            try:
                async with async_session() as session:
                    await session.execute(
                        text("""
                            INSERT INTO error_logs
                                (error_type, error_message, stack_trace)
                            VALUES
                                (:error_type, :error_message, :stack_trace)
                        """),
                        {
                            "error_type": error_type,
                            "error_message": error_msg_raw,
                            "stack_trace": tb[:4000],
                        },
                    )
                    await session.commit()
            except Exception as db_err:
                logger.error("Failed to persist error log: %s", db_err)

            # §17.183: typed upstream-down classification. Pre-§17.183 every
            # unhandled exception bubbled to a generic 500 "Internal Server
            # Error" — an operator debugging a research call had to grep
            # docker logs + cross-reference timestamps to tell SearXNG-down
            # from Milvus-collection-missing. Now: transport failures to
            # known upstream URLs surface as 503 with {service, hint} so the
            # category is on the wire. Stack traces stay redacted; the
            # error_logs row is unchanged.
            upstream = _classify_upstream(exc)
            if upstream is not None:
                service, hint = upstream
                return JSONResponse(
                    status_code=503,
                    content={
                        "error": "upstream_unreachable",
                        "service": service,
                        "hint": hint,
                        "path": request.url.path,
                    },
                )

            return JSONResponse(
                status_code=500,
                content={
                    "error": type(exc).__name__,
                    "message": error_msg_safe,
                    "path": request.url.path,
                },
            )


def _classify_upstream(exc: Exception) -> tuple[str, str] | None:
    """Map known upstream transport failures to (service, hint), or None.

    Returns a tuple when the exception carries enough information to identify
    the failing upstream:

      * ``pymilvus.MilvusException`` → ("milvus", …) — Milvus speaks gRPC,
        not httpx, so its failures arrive as a separate exception type.
      * ``httpx.TransportError`` (connect / timeout / network / protocol)
        whose request URL host matches ``settings.searxng_url``,
        ``settings.ollama_base_url``, or ``settings.milvus_uri`` → the
        respective service. ``httpx.HTTPStatusError`` is intentionally NOT
        classified — a 5xx response means the upstream IS reachable and
        chose to return an error, which is a different operator concern.

    Returns None for anything else; the middleware then falls back to a
    generic 500.
    """
    if isinstance(exc, MilvusException):
        return ("milvus", "GET /health or check the scaffold-milvus container")

    if isinstance(exc, httpx.TransportError):
        # ``httpx.RequestError.request`` is a property that RAISES
        # RuntimeError when no request was bound to the exception (e.g.
        # pool-time failures). ``getattr(..., None)`` would let that
        # RuntimeError escape ``_classify_upstream`` and crash the
        # middleware — so wrap the access explicitly.
        try:
            req = exc.request
        except RuntimeError:
            return None
        host = getattr(getattr(req, "url", None), "host", None)
        if not host:
            return None

        for url, service in (
            (settings.searxng_url, "searxng"),
            (settings.ollama_base_url, "ollama"),
            (settings.milvus_uri, "milvus"),
        ):
            cfg_host = urlparse(url).hostname
            if cfg_host and host == cfg_host:
                return (
                    service,
                    f"GET /health or check the scaffold-{service} container",
                )

    return None


def _classify_error(exc: Exception) -> str:
    """Map exception type to error_type enum value.

    ValueError and KeyError typically reflect bad user input or missing
    expected keys at boundaries; TypeError is almost always a programmer
    error (wrong arg count / type), so it joins the unrecoverable bucket
    rather than being misreported as user-validation noise.
    """
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.HTTPError):
        return "transient"
    if isinstance(exc, (ValueError, KeyError)):
        return "validation"
    return "unrecoverable"
