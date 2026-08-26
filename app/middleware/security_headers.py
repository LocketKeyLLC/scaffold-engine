"""§17.97 — security response headers for HTML-serving routes.

Adds a Content-Security-Policy header to every response on
``/web/*``, ``/research/pdf``, and ``/ui*`` (the HTML-serving prefixes;
``/ui`` is the standalone operator SPA — all-external JS/CSS, no inline,
so ``script-src 'self'``/``style-src 'self'`` pass without a nonce).
Non-HTML responses (JSON API, SSE streams, /metrics, /health) are
left alone — CSP only meaningfully constrains how a BROWSER renders
a response, so attaching it to API responses is noise.

The policy is strict — no ``'unsafe-inline'`` anywhere:
  * ``script-src 'self' 'nonce-<n>'`` — §17.459 self-hosted HTMX + the
    SSE extension under ``/static/vendor/`` (was unpkg.com); §17.460
    replaced 'unsafe-inline' with a per-request nonce. Inline ``<script>``
    elements must carry ``nonce="{{ request.state.csp_nonce }}"``.
  * ``style-src 'self' 'nonce-<n>'`` — external CSS (web.css) + nonce'd
    inline ``<style>`` elements only. Inline ``style=`` attributes are NOT
    permitted (a nonce can't cover an attribute) — use classes instead.
    HTMX's auto-injected indicator ``<style>`` is disabled via the
    ``htmx-config`` meta in _layout.html so it doesn't trip the policy.
  * ``img-src 'self' data:`` — data URIs for embedded SVG icons.
  * ``object-src 'none'`` — kills Flash/embed/object surface.
  * ``frame-ancestors 'none'`` — clickjacking defense; the UI
    is operator-only and never legitimately embedded.
  * ``base-uri 'self'`` — prevents ``<base href>`` redirects.

The nonce is minted per request by this middleware (``request.state.
csp_nonce``) and must match the value the template stamps on its inline
elements — see ``_build_csp`` + the dispatch method below.

A trivially-related header is also set:
  * ``X-Content-Type-Options: nosniff`` — defense against MIME
    sniffing.
  * ``Referrer-Policy: same-origin`` — outbound links don't leak
    the orchestrator URL to third parties.
"""
from __future__ import annotations

import secrets
from contextvars import ContextVar

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint


_HTML_PREFIXES = ("/web/", "/research/pdf", "/ui")

# §17.460 — per-request CSP nonce carried in a ContextVar. Set in dispatch
# BEFORE call_next, so anyio copies it into the endpoint's task context and the
# template (via the Jinja `csp_nonce` global → current_csp_nonce) reads the same
# value the header advertises. request.state does NOT survive the
# BaseHTTPMiddleware → endpoint hop here (scope snapshot), but a contextvar set
# pre-call_next does — same mechanism the request_id middleware relies on.
_CSP_NONCE: ContextVar[str] = ContextVar("csp_nonce", default="")


def current_csp_nonce() -> str:
    """Return the current request's CSP nonce ("" outside an HTML request).

    Registered as the Jinja `csp_nonce` global so templates can stamp inline
    ``<script>``/``<style>`` elements with ``nonce="{{ csp_nonce() }}"``.
    """
    return _CSP_NONCE.get()

# §17.460 — CSP directives. script-src/style-src carry a per-request
# 'nonce-{nonce}' instead of 'unsafe-inline': only inline <script>/<style>
# ELEMENTS stamped with the matching nonce are admitted (inline style=/on*=
# ATTRIBUTES are never allowed — templates must use classes/external files).
_CSP_DIRECTIVES = (
    "default-src 'self'",
    "script-src 'self' 'nonce-{nonce}'",
    "style-src 'self' 'nonce-{nonce}'",
    "img-src 'self' data:",
    "connect-src 'self'",
    "font-src 'self' data:",
    "object-src 'none'",
    "frame-ancestors 'none'",
    "base-uri 'self'",
)


def _build_csp(nonce: str) -> str:
    """Render the CSP header value for a given per-request nonce."""
    return "; ".join(d.format(nonce=nonce) for d in _CSP_DIRECTIVES)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Apply CSP + nosniff + same-origin Referrer-Policy to HTML routes."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        is_html = request.url.path.startswith(_HTML_PREFIXES)
        # Set the nonce BEFORE call_next so the endpoint's template sees it via
        # the copied task context (see _CSP_NONCE note above). Reset in finally
        # so the value never leaks into an unrelated request on this worker.
        token = _CSP_NONCE.set(secrets.token_urlsafe(16)) if is_html else None
        try:
            response = await call_next(request)
            if is_html:
                response.headers.setdefault(
                    "Content-Security-Policy", _build_csp(_CSP_NONCE.get()),
                )
                response.headers.setdefault("X-Content-Type-Options", "nosniff")
                response.headers.setdefault("Referrer-Policy", "same-origin")
            # §17.842 — the SPA's ES modules were served with ETag/Last-Modified
            # but NO Cache-Control, so browsers heuristic-cached them and a
            # plain reload kept running stale UI after a deploy (bit the
            # operator twice: §17.840 round 7 and again post-release).
            # no-cache = always revalidate (304 when unchanged), never stale.
            if request.url.path.startswith(("/ui/", "/static/")):
                response.headers.setdefault("Cache-Control", "no-cache")
            return response
        finally:
            if token is not None:
                _CSP_NONCE.reset(token)
