"""Request-ID middleware.

Binds a ``request_id`` contextvar on every incoming request so every log
line emitted during the request — from perf middleware, error logging,
ORM, HTTP clients, model_router, etc. — carries the same correlation ID
without call-site changes. Works with the already-installed
``structlog.contextvars.merge_contextvars`` in the logging chain.

Install order (main.py): RequestIdMiddleware must run BEFORE the
performance and error-logging middlewares so those records inherit the
bound value.

Honors an inbound ``X-Request-ID`` header when present (client-supplied
trace ID for cross-service correlation); otherwise generates a UUID4.
"""
from __future__ import annotations

import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

import structlog

_HEADER = "X-Request-ID"


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        incoming = request.headers.get(_HEADER)
        request_id = incoming if incoming else uuid.uuid4().hex
        # Bind for the duration of this request; clear on exit so contextvars
        # don't leak across tasks pooled by the server.
        token = structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            # bind_contextvars returns a dict, not a token; use clear_contextvars
            # to drop bindings set in this frame.
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers[_HEADER] = request_id
        return response
