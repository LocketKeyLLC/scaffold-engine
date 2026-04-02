"""Scaffold Engine — Performance logging middleware.

Two components:
  1. HTTP middleware: logs request duration for all endpoints
  2. log_model_call(): persists model-level metrics from ModelResponse to performance_logs

Step 9 of 23-step build plan.
"""

from __future__ import annotations

import logging
import time

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from sqlalchemy import text

from app.database import async_session

logger = logging.getLogger("scaffold.perf")


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
        elapsed_ms = int((time.monotonic() - start) * 1000)

        logger.info(
            "%s %s → %d (%dms)",
            request.method, request.url.path,
            response.status_code, elapsed_ms,
        )

        # Add timing header for observability
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
                    "model": model,
                    "endpoint": endpoint,
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
