"""Scaffold Engine — Performance logging middleware.

Logs the wall-clock duration of every HTTP request. Emits both
duration_ms (int) and duration_s (float) so percentile aggregation
across log stores stays straightforward. /health polling is gated to
DEBUG when fast (below threshold) and INFO when slow.

History note: this module also used to expose ``log_model_call()``,
which persisted per-LLM-call metrics to a ``performance_logs`` table.
J.3.a (migration 030) replaced that path with ``_record_call`` writing
to ``llm_call_logs``; the helper became dead code immediately and the
table accumulated nothing thereafter. Both were dropped by X.22 +
migration 031.
"""
from __future__ import annotations

import logging
import time

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import StreamingResponse

logger = logging.getLogger("scaffold.perf")

# /health polling: log at DEBUG when duration is below threshold, INFO above.
_HEALTH_PATH = "/health"
_HEALTH_SLOW_MS = 200


# ---------------------------------------------------------------------------
# HTTP request timing middleware
# ---------------------------------------------------------------------------
class PerformanceMiddleware(BaseHTTPMiddleware):
    """Log wall-clock duration of every HTTP request."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        start = time.monotonic()
        response = await call_next(request)
        elapsed_s = time.monotonic() - start
        elapsed_ms = int(elapsed_s * 1000)

        path = request.url.path
        # Gate noisy /health polling: DEBUG below threshold, INFO above.
        if path == _HEALTH_PATH and elapsed_ms < _HEALTH_SLOW_MS:
            level = logging.DEBUG
        else:
            level = logging.INFO

        logger.log(
            level,
            "http_request_completed: method=%s path=%s status=%d "
            "duration_ms=%d duration_s=%.3f",
            request.method, path,
            response.status_code, elapsed_ms, elapsed_s,
        )
        # Streaming responses have already emitted headers when call_next
        # returns; mutating ``response.headers`` here is a silent no-op for
        # the wire. Skip the header set so the intent is clear.
        if not isinstance(response, StreamingResponse):
            response.headers["X-Request-Duration-Ms"] = str(elapsed_ms)
        return response
