"""API key authentication dependency for Scaffold Engine."""
import secrets
import logging
from fastapi import Request, Security, HTTPException, status
from fastapi.security import APIKeyHeader

from app.config import settings

_logger = logging.getLogger(__name__)

_RAW_KEY = settings.scaffold_api_key.get_secret_value()

if not _RAW_KEY:
    if not settings.scaffold_auth_disabled:
        raise RuntimeError(
            "SCAFFOLD_API_KEY is empty. Set it, or set SCAFFOLD_AUTH_DISABLED=1 "
            "to run without authentication (NOT recommended outside local dev)."
        )
    _logger.warning(
        "SCAFFOLD_AUTH_DISABLED=1 — API authentication is OFF. Do not run this way in prod."
    )

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

# Paths exempt from API-key auth. Health probes need to be accessible
# without credentials so external orchestrators (compose healthchecks,
# uptime pingers, etc.) can read the dependency status. Adding a future
# /healthz alias is a one-line set extension here.
#
# X.26: ``settings.metrics_path`` is exempted because Prometheus scrapers
# don't carry our X-API-Key header by convention, and the surface is
# read-only counters/gauges with no PII (matches the /health rationale).
# The set is built at module load — if metrics_path is reconfigured, the
# orchestrator must restart for the exemption to track.
_AUTH_EXEMPT_PATHS = frozenset({"/health", "/", settings.metrics_path})

# Sprint J.2.a — prefix-based auth exemption. The native web UI lives at
# ``/web/*`` and serves a browsable page (operators don't pass headers in
# a browser); the /static mount serves the UI's CSS. The embedded SDK
# Client carries the API key for the loopback HTTP call to the actual
# orchestrator endpoints, so end-to-end auth is preserved — only the
# browser-facing layer is exempt.
_AUTH_EXEMPT_PREFIXES = ("/web/", "/static/")


async def require_api_key(
    request: Request,
    key: str | None = Security(api_key_header),
) -> str:
    """Validate X-API-Key header. Returns the key on success, raises 401 on failure."""
    path = request.url.path
    if path in _AUTH_EXEMPT_PATHS:
        return ""
    if any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
        return ""

    # Explicit opt-out only — empty key with no opt-out would have raised at import
    if settings.scaffold_auth_disabled:
        return ""

    # §17.596 — HTTP headers decode as latin-1, so a non-ASCII byte in the
    # X-API-Key value yields a str that secrets.compare_digest rejects with
    # `TypeError: comparing strings with non-ASCII characters`. Left uncaught
    # it escapes the auth dependency as a 500 + an error_logs row (and can trip
    # the unresolved-errors watchdog at threshold=1) instead of the intended
    # 401 — the same failure class §17.441 fixed for RecursionError. Treat the
    # TypeError as a failed comparison.
    try:
        ok = key is not None and secrets.compare_digest(key, _RAW_KEY)
    except TypeError:
        ok = False
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return key
