"""§17.97 — global request body size cap.

Pre-§17.97, the orchestrator relied on Pydantic per-field caps + the
per-endpoint PDF cap. No GLOBAL byte cap meant a token-holder posting
an oversized JSON body to (e.g.) /optimize or /ideate could exhaust
memory before any per-endpoint validation ran. This middleware
short-circuits the request at Content-Length parse time with a 413.

Limitations:
  * Content-Length can be spoofed; chunked-transfer-encoding bodies
    skip this check entirely. Defense-in-depth via uvicorn's
    --h11-max-incomplete-event-size would close that, but is not
    configurable from FastAPI code. For the single-operator threat
    model the Content-Length pre-check is sufficient.
  * The PDF endpoint has its own larger cap (research_max_pdf_bytes,
    20 MB by default) — this middleware skips /research/pdf so the
    upload flow keeps working.
"""
from __future__ import annotations

from fastapi import Request, Response
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from app.config import settings


# Paths that legitimately accept larger bodies; bypass the global cap.
_BYPASS_PREFIXES = ("/research/pdf",)


class BodySizeLimitMiddleware(BaseHTTPMiddleware):
    """Reject requests whose Content-Length exceeds ``settings.max_request_body_bytes``."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.url.path.startswith(_BYPASS_PREFIXES):
            return await call_next(request)
        cl = request.headers.get("content-length")
        if cl and cl.isdigit():
            cap = settings.max_request_body_bytes
            if int(cl) > cap:
                return JSONResponse(
                    {"detail": f"Request body exceeds {cap}-byte cap"},
                    status_code=413,
                )
        return await call_next(request)
