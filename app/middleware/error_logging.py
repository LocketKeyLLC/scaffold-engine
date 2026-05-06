"""Scaffold Engine — Error logging middleware.

Catches unhandled exceptions from any endpoint, logs to error_logs table,
and returns a structured error response.

Step 8 of 23-step build plan.
"""

from __future__ import annotations

import logging
import traceback
import httpx

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse
from sqlalchemy import text

from app.database import async_session

logger = logging.getLogger("scaffold.errors")


class ErrorLoggingMiddleware(BaseHTTPMiddleware):
    """Intercept unhandled exceptions → write to error_logs → return 500."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        try:
            return await call_next(request)
        except Exception as exc:
            tb = traceback.format_exc()
            error_msg = str(exc)[:1000]
            logger.error(
                "http_request_failed: exception=%s method=%s path=%s error=%s",
                type(exc).__name__, request.method, request.url.path, error_msg,
            )

            # Classify error type
            error_type = _classify_error(exc)

            # Persist to error_logs (fire-and-forget, don't let logging fail the response)
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
                            "error_message": error_msg,
                            "stack_trace": tb[:4000],
                        },
                    )
                    await session.commit()
            except Exception as db_err:
                logger.error("Failed to persist error log: %s", db_err)

            return JSONResponse(
                status_code=500,
                content={
                    "error": type(exc).__name__,
                    "message": error_msg,
                    "path": request.url.path,
                },
            )


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
