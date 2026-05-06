"""Scaffold Engine — Performance logging middleware.

Two components:
  1. HTTP middleware: logs request duration for all endpoints. Emits both
     duration_ms (int) and duration_s (float) to make percentile aggregation
     across log stores straightforward. /health polling is gated to DEBUG
     when fast (below threshold) and INFO when slow.
  2. log_model_call(): persists model-level metrics to performance_logs.
     model/endpoint strings are truncated to column widths before insert.
"""
from __future__ import annotations

import logging
import time

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import StreamingResponse
from sqlalchemy import text

from app.database import async_session

logger = logging.getLogger("scaffold.perf")

# Column width guards for performance_logs text columns.
_MODEL_MAX = 200
_ENDPOINT_MAX = 200

# /health polling: log at DEBUG when duration is below threshold, INFO above.
_HEALTH_PATH = "/health"
_HEALTH_SLOW_MS = 200


def _truncate(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value if len(value) <= limit else value[: limit - 1] + "…"


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


# ---------------------------------------------------------------------------
# Model call metric persistence
# ---------------------------------------------------------------------------
async def log_model_call(
    *,
    model: str,
    endpoint: str,
    request_type: str = "generate",
    ttft_ms: int | None = None,
    total_duration_ms: int = 0,
    tokens_prompt: int | None = None,
    tokens_completion: int | None = None,
    tokens_per_sec: float | None = None,
    success: bool = True,
    error_message: str | None = None,
    job_id: str | None = None,
    node_id: str | None = None,
) -> None:
    """Persist a model call's performance metrics to performance_logs.

    Called by model_router after each Ollama dispatch.
    """
    try:
        async with async_session() as session:
            await session.execute(
                text("""
                    INSERT INTO performance_logs
                        (model, endpoint, request_type, ttft_ms,
                         total_duration_ms, tokens_prompt, tokens_completion,
                         tokens_per_sec, success, error_message,
                         job_id, node_id)
                    VALUES
                        (:model, :endpoint, :request_type, :ttft_ms,
                         :total_duration_ms, :tokens_prompt, :tokens_completion,
                         :tokens_per_sec, :success, :error_message,
                         :job_id, :node_id)
                """),
                {
                    "model": _truncate(model, _MODEL_MAX),
                    "endpoint": _truncate(endpoint, _ENDPOINT_MAX),
                    "request_type": request_type,
                    "ttft_ms": ttft_ms,
                    "total_duration_ms": total_duration_ms,
                    "tokens_prompt": tokens_prompt,
                    "tokens_completion": tokens_completion,
                    "tokens_per_sec": tokens_per_sec,
                    "success": success,
                    "error_message": error_message[:500] if error_message else None,
                    "job_id": job_id,
                    "node_id": node_id,
                },
            )
            await session.commit()
    except Exception as e:
        logger.error("Failed to persist perf log: %s", e)
