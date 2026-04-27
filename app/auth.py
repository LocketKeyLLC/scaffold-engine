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


async def require_api_key(
    request: Request,
    key: str | None = Security(api_key_header),
) -> str:
    """Validate X-API-Key header. Returns the key on success, raises 401 on failure."""
    # Health checks bypass auth
    if request.url.path == "/health":
        return ""

    # Explicit opt-out only — empty key with no opt-out would have raised at import
    if settings.scaffold_auth_disabled:
        return ""

    if key is None or not secrets.compare_digest(key, _RAW_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return key
