"""§17.812 (audit C9) — same-origin CSRF guard for the server-rendered /web UI.

The ``/web/*`` pages are browser-facing and (by design) drive admin-privileged
writes through an internal loopback — but browsers don't attach ``X-API-Key``,
so the surface is auth-exempt (see ``app/auth.py`` ``_AUTH_EXEMPT_PREFIXES``).
That leaves it open to cross-origin CSRF: a malicious page the operator visits
can auto-POST to ``http://127.0.0.1:8000/web/…`` and trigger
ideate / confirm(→execute) / model-set / node-delete with no credential.

This middleware applies the standard same-origin defense to STATE-CHANGING
methods on ``/web/*``: if the request carries an ``Origin`` (or, failing that,
a ``Referer``) header whose host:port does not match the request's own Host, it
is refused with 403 before any handler runs. A cross-origin browser form POST
always sends ``Origin``, so the real attack is caught; a same-origin ``/web``
form post matches and passes.

Scoped to ``/web/*`` only — the JSON API and the ``/ui`` SPA authenticate with a
header (not a cookie), so they are not CSRF-able and are left untouched. This is
a stopgap; ``/web`` is slated for retirement (plan Phase 5.9), which removes the
surface entirely.
"""
from __future__ import annotations

import logging
from urllib.parse import urlsplit

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.responses import JSONResponse

logger = logging.getLogger("scaffold")

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def _netloc_of(value: str) -> str:
    """Return the lowercased host[:port] of an Origin/Referer value ("" if none).

    ``Origin`` is a bare ``scheme://host[:port]``; ``Referer`` is a full URL.
    ``urlsplit().netloc`` extracts host[:port] from either.
    """
    v = value.strip()
    if not v:
        return ""
    return urlsplit(v).netloc.lower()


class WebCsrfMiddleware(BaseHTTPMiddleware):
    """Refuse cross-origin state-changing requests to ``/web/*`` (403)."""

    async def dispatch(
        self, request: Request, call_next: RequestResponseEndpoint,
    ) -> Response:
        if request.method in _UNSAFE_METHODS and request.url.path.startswith("/web/"):
            request_host = (request.headers.get("host") or "").lower()
            origin = request.headers.get("origin")
            referer = request.headers.get("referer")
            claimed = _netloc_of(origin) if origin else _netloc_of(referer or "")
            # Reject only when a cross-origin signal is PRESENT and mismatched.
            # A cross-origin attacker's browser always sends Origin on POST, so
            # the real CSRF is caught; requests with neither header (non-browser
            # tooling) fall through to the normal auth path unchanged.
            if claimed and request_host and claimed != request_host:
                logger.warning(
                    "web_csrf_rejected: path=%s method=%s host=%s claimed=%s",
                    request.url.path, request.method, request_host, claimed,
                )
                return JSONResponse(
                    {"detail": "Cross-origin request refused (CSRF guard)"},
                    status_code=403,
                )
        return await call_next(request)
