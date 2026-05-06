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
trace ID for cross-service correlation), but only after sanitizing so a
caller cannot inject newlines, control chars, or arbitrary length into
structured logs. Otherwise generates a UUID4.
"""
from __future__ import annotations

import re
import uuid

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

import structlog

_HEADER = "X-Request-ID"

# Inbound X-Request-ID is accepted only when it matches a conservative
# alphanumeric / dash / underscore charset and fits in 64 chars. Anything
# else is replaced with a fresh UUID4 hex — no log injection vector, and
# a 32-hex-char UUID always satisfies the regex on the round-trip.
_VALID_REQUEST_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class RequestIdMiddleware(BaseHTTPMiddleware):
    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        incoming = request.headers.get(_HEADER)
        if incoming and _VALID_REQUEST_ID.match(incoming):
            request_id = incoming
        else:
            request_id = uuid.uuid4().hex
        # Bind for the duration of this request; clear on exit so contextvars
        # don't leak across tasks pooled by the server.
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
        finally:
            # bind_contextvars returns a dict, not a token; use unbind to
            # drop the binding set in this frame.
            structlog.contextvars.unbind_contextvars("request_id")
        response.headers[_HEADER] = request_id
        return response
