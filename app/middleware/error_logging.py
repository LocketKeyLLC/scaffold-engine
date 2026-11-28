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
import structlog
from sqlalchemy import exc as sa_exc

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse
from sqlalchemy import text

try:
    from pymilvus.exceptions import MilvusException  # pyright: ignore[reportAssignmentType] — fallback class below
except ImportError:  # pragma: no cover — pymilvus is required, but keep the
    # import defensive so a stripped install can still load the middleware.
    class MilvusException(Exception):  # type: ignore[no-redef]  # pyright: ignore[reportRedeclaration]
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


_TB_MAX = 4000
_TB_HEAD = 1000
_TB_MARK = "\n  ... [{n} chars truncated — head and tail kept] ...\n"


def _bounded_traceback(tb: str) -> str:
    """§17.2068 — bound a traceback to ~_TB_MAX chars keeping its TAIL.

    ``tb[:4000]`` kept the head and cut the final line naming the exception
    plus the innermost frames — the part that says what failed. Keep the first
    _TB_HEAD chars (where it entered) and the rest from the end.
    """
    if len(tb) <= _TB_MAX:
        return tb
    tail = _TB_MAX - _TB_HEAD
    cut = len(tb) - _TB_HEAD - tail
    return tb[:_TB_HEAD] + _TB_MARK.format(n=cut) + tb[-tail:]


def _describe_exception(exc: BaseException) -> str:
    """§17.2068 — ``Type: message``, never empty.

    ``str(TimeoutError())`` is ``''`` — the live rows stored nothing. The type
    name is always given; an empty message says so, and the direct cause's type
    is named (asyncio.timeout raises ``TimeoutError from CancelledError``).
    """
    msg = str(exc).strip()
    out = f"{type(exc).__name__}: {msg}" if msg else f"{type(exc).__name__}: <no message>"
    cause = exc.__cause__
    if cause is not None:
        cmsg = str(cause).strip()
        out += f" (caused by {type(cause).__name__}" + (f": {cmsg})" if cmsg else ")")
    return out


class ErrorLoggingMiddleware(BaseHTTPMiddleware):
    """Intercept unhandled exceptions → write to error_logs → return 500/503."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        # §17.2068 — only ``Exception`` is caught: a cancelled request (client
        # disconnect, server shutdown) raises ``CancelledError``, a
        # BaseException that Starlette's call_next does not capture either, so
        # it propagates untouched and is never recorded as a 500.
        try:
            return await call_next(request)
        except Exception as exc:
            tb = _bounded_traceback(traceback.format_exc())
            what = _describe_exception(exc)
            request_id = structlog.contextvars.get_contextvars().get("request_id")
            # §17.2068 — the row names the request: error_logs has no
            # method/path column, so the message carries them (path only —
            # a query string may hold a credential).
            where = f"{request.method} {request.url.path}"
            if request_id:
                where += f" [request_id={request_id}]"
            error_msg_raw = f"{where}: {what}"[:1000]
            error_msg_safe = _redact_secrets(error_msg_raw)
            logger.error(
                "http_request_failed: exception=%s method=%s path=%s error=%s",
                type(exc).__name__, request.method, request.url.path,
                _redact_secrets(what[:1000]),
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
                            "stack_trace": tb,
                        },
                    )
                    await session.commit()
            except Exception as db_err:
                # §17.263 — DB write failure is exactly when we most need
                # the trace; pre-fix this branch logged only db_err and
                # dropped the original context (error_type, request path,
                # traceback). Now emit a self-contained fallback entry so
                # operators reading journald don't lose the bridge to the
                # original failure. Traceback is redacted because journald
                # has broader read access than auth-gated /observability/errors.
                logger.error(
                    "error_log_db_write_failed: db_error=%s | "
                    "original_error_type=%s method=%s path=%s original_error=%s",
                    db_err, error_type, request.method, request.url.path,
                    error_msg_safe,
                )
                logger.error(
                    "error_log_db_write_failed_traceback:\n%s",
                    _redact_secrets(tb),
                )

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
                    "message": _redact_secrets(what[:1000]),
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
        # §17.207 — also compare port. Production hosts are all distinct
        # (172.18.0.1 / searxng / milvus-standalone) so host-only was
        # sufficient, but cloud-CI overrides every upstream to
        # ``http://localhost:<port>``, collapsing the host tuples and
        # making the iteration order silently win (searxng-first → ollama
        # errors mis-classified as searxng). Comparing (host, port) keeps
        # production behavior identical while making the test/CI deploy
        # disambiguation correct.
        port = getattr(getattr(req, "url", None), "port", None)

        for url, service in (
            (settings.searxng_url, "searxng"),
            (settings.ollama_base_url, "ollama"),
            (settings.milvus_uri, "milvus"),
        ):
            parsed = urlparse(url)
            cfg_host = parsed.hostname
            cfg_port = parsed.port
            if cfg_host and host == cfg_host and (
                cfg_port is None or port is None or cfg_port == port
            ):
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
    # §17.2068 — the builtin TimeoutError (asyncio.timeout's, which asyncpg's
    # connect raises when Postgres does not answer within connect_args
    # timeout) and SQLAlchemy's pool-checkout timeout are timeouts, not
    # programmer errors. A refused/reset connection is transient.
    if isinstance(exc, (TimeoutError, sa_exc.TimeoutError)):
        return "timeout"
    if isinstance(exc, ConnectionError):
        return "transient"
    if isinstance(exc, (ValueError, KeyError)):
        return "validation"
    return "unrecoverable"
