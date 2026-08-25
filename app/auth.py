"""API key authentication dependency for Scaffold Engine."""
import secrets
import logging
from fastapi import Request, Security, HTTPException, status
from fastapi.security import APIKeyHeader

from app.config import settings
from app.database import async_session
from app.authz import ADMIN_PRINCIPAL, principal_for_key_row
from app.modules.api_keys import resolve_key

_logger = logging.getLogger(__name__)

_RAW_KEY = settings.scaffold_api_key.get_secret_value()

if not _RAW_KEY:
    # §17.807 — multi-user installs MAY run without a master key (auth then
    # rests entirely on minted scoped keys), so an empty master is only fatal
    # when neither the explicit opt-out nor multi-user mode is in force.
    if not settings.scaffold_auth_disabled and not settings.multi_user_enabled:
        raise RuntimeError(
            "SCAFFOLD_API_KEY is empty. Set it, set MULTI_USER_ENABLED=true and "
            "mint scoped keys (make key-add), or set SCAFFOLD_AUTH_DISABLED=1 to "
            "run without authentication (NOT recommended outside local dev)."
        )
    if settings.scaffold_auth_disabled:
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
#
# ``/ui/*`` is the standalone operator SPA (no-build static assets). Only
# the asset-serving layer is exempt — the SPA itself sends X-API-Key on
# every API call (it reads it from browser localStorage), so the API
# surface behind it stays fully gated, same guarantee as the /web loopback.
_AUTH_EXEMPT_PREFIXES = ("/web/", "/static/", "/ui/")


async def require_api_key(
    request: Request,
    key: str | None = Security(api_key_header),
) -> str:
    """Validate X-API-Key header. Returns the key on success, raises 401 on failure."""
    path = request.url.path
    if path in _AUTH_EXEMPT_PATHS:
        return ""
    if any(path.startswith(p) for p in _AUTH_EXEMPT_PREFIXES):
        # §17.820b — the §17.812 (audit C9) multi-user gate on /web is gone
        # WITH ITS REASON: /web served admin-view HTML whose loopback
        # re-authenticated as the master key, so under MULTI_USER_ENABLED an
        # unauthenticated browser saw every user's jobs. Since the §17.820
        # retirement /web is 301 redirects ONLY (static SPA Locations, no
        # data), so old bookmarks must land on the SPA login in every mode —
        # not a bare 401. The whole /web/ prefix (and this exemption entry)
        # is deleted one release after the redirects.
        # /static + /ui asset serving stay exempt (the SPA sends its own key).
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
    #
    # Master key is checked first (constant-time) and, on match, authenticates
    # as the admin/bootstrap key regardless of mode. The ``_RAW_KEY`` guard
    # prevents an empty master (allowed in multi-user mode) from matching an
    # empty/omitted header via compare_digest("", "").
    try:
        is_admin = bool(_RAW_KEY) and key is not None and secrets.compare_digest(key, _RAW_KEY)
    except TypeError:
        is_admin = False
    if is_admin:
        # §17.810 — the master key authenticates as the admin principal. Attach
        # it so downstream handlers (get_principal) see full-visibility identity.
        request.state.principal = ADMIN_PRINCIPAL
        return key

    # §17.807 — multi-user mode: a presented key that isn't the master may still
    # be a live scoped key (api_keys, mig 066), matched by SHA-256 digest. The
    # DB is consulted only here (non-admin key + multi-user on), so single-user
    # installs keep the pure in-memory compare above with no per-request query.
    #
    # §17.810 — resolve to the key's Principal (identity from its owner tag, role
    # from the row) and attach it to request.state so job endpoints can scope by
    # owner. A missing/revoked key yields None → falls through to 401.
    if settings.multi_user_enabled and key:
        try:
            async with async_session() as session:
                row = await resolve_key(session, key)
                if row is not None:
                    request.state.principal = principal_for_key_row(row)
                    return key
        except TypeError:
            # Non-ASCII header byte reaches hashlib as-is (utf-8 encodable), so
            # this is unreachable in practice; kept symmetric with the master
            # path so a lookup fault still degrades to a clean 401, not a 500.
            pass

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or missing API key",
    )


# §17.788 — auth for the native OpenAI surface (/v1/*). OpenAI clients send the
# key as ``Authorization: Bearer <key>``, not ``X-API-Key``; the /v1 sub-app is
# mounted (bypassing the global require_api_key dependency), so it carries this
# guard instead. Accepts EITHER a Bearer token or X-API-Key against the same
# SCAFFOLD_API_KEY — the /ui SPA already sends X-API-Key, external OpenAI clients
# send Bearer. The 401 is raised as an OpenAI-shaped envelope by the /v1 sub-app's
# exception handler (openai_compat.py), which stock OpenAI SDKs expect.
_authorization_header = APIKeyHeader(name="Authorization", auto_error=False)


def _bearer_token(raw: str | None) -> str | None:
    """Extract the token from an ``Authorization: Bearer <token>`` value.

    Tolerant of the ``Bearer `` prefix (case-insensitive) and of a bare token.
    Returns None for an empty/malformed header.
    """
    if not raw:
        return None
    parts = raw.split(None, 1)
    if len(parts) == 2 and parts[0].lower() == "bearer":
        return parts[1].strip() or None
    return raw.strip() or None


async def require_openai_key(
    bearer: str | None = Security(_authorization_header),
    x_api_key: str | None = Security(api_key_header),
) -> str:
    """Validate the caller for the native OpenAI surface.

    Accepts ``Authorization: Bearer <SCAFFOLD_API_KEY>`` OR ``X-API-Key`` against
    the same key. Returns the matched key on success; raises 401 on failure (the
    /v1 sub-app formats it as an OpenAI ``{"error": {...}}`` envelope).

    §17.810 — ADMIN-ONLY BY DESIGN under multi-user. This compares only against
    the master key, so scoped keys get 401 here. That is deliberate: the /v1
    handlers reach the job pipeline through the native_chat in-process loopback
    (``app/native_chat/engine_client.py``), which re-authenticates as the master
    key and cannot forward a per-caller identity. Accepting scoped keys without
    that forwarding would let a non-admin create admin-owned jobs and list every
    user's work — a privilege escalation. Per-user multi-user access is via the
    direct JSON API (which resolves a Principal) and the /ui SPA (which sends
    X-API-Key). Forwarding identity through the loopback is future work.
    """
    if settings.scaffold_auth_disabled:
        return ""

    candidate = _bearer_token(bearer) or x_api_key
    try:
        # §17.812 (audit LOW) — bool(_RAW_KEY) mirrors require_api_key's master
        # path (:87): under MULTI_USER_ENABLED the master key may be empty, and
        # without this guard an empty candidate would compare "" == "" and pass.
        # _bearer_token already returns None for empty input, so this is
        # defense-in-depth against a future change there — kept symmetric.
        ok = (
            candidate is not None
            and bool(_RAW_KEY)
            and secrets.compare_digest(candidate, _RAW_KEY)
        )
    except TypeError:
        # §17.596 — non-ASCII header bytes make compare_digest raise; treat as fail.
        ok = False
    if not ok:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return candidate
