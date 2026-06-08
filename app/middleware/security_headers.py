"""§17.97 — security response headers for HTML-serving routes.

Adds a Content-Security-Policy header to every response on
``/web/*`` and ``/research/pdf`` (the two HTML-serving prefixes).
Non-HTML responses (JSON API, SSE streams, /metrics, /health) are
left alone — CSP only meaningfully constrains how a BROWSER renders
a response, so attaching it to API responses is noise.

The policy is intentionally permissive enough to keep the current
HTMX-based UI functional without per-template changes:
  * ``script-src 'self' 'unsafe-inline'`` — §17.459: HTMX + the SSE
    extension are now self-hosted under ``/static/vendor/`` (was
    unpkg.com), so the external origin is dropped from script-src.
    'unsafe-inline' covers any future inline ``<script>`` blocks
    so a strict CSP can't break the UI without a code change.
  * ``style-src 'self' 'unsafe-inline'`` — HTMX templates use
    inline ``style=`` attributes in places.
  * ``img-src 'self' data:`` — data URIs for embedded SVG icons.
  * ``object-src 'none'`` — kills Flash/embed/object surface.
  * ``frame-ancestors 'none'`` — clickjacking defense; the UI
    is operator-only and never legitimately embedded.
  * ``base-uri 'self'`` — prevents ``<base href>`` redirects.

Operators tightening this further should drop 'unsafe-inline' next
(audit + nonce-ize inline scripts/styles). unpkg.com was removed in
§17.459 by self-hosting HTMX under /static/vendor/.

A trivially-related header is also set:
  * ``X-Content-Type-Options: nosniff`` — defense against MIME
    sniffing.
  * ``Referrer-Policy: same-origin`` — outbound links don't leak
    the orchestrator URL to third parties.
"""
from __future__ import annotations

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


_HTML_PREFIXES = ("/web/", "/research/pdf")

_CSP = "; ".join((
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "font-src 'self' data:",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
))


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply CSP + nosniff + same-origin Referrer-Policy to HTML routes."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        response = await call_next(request)
        path = request.url.path
        if path.startswith(_HTML_PREFIXES) or path == "/research/pdf":
            response.headers.setdefault("Content-Security-Policy", _CSP)
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("Referrer-Policy", "same-origin")
        return response
