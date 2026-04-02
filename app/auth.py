"""API key authentication dependency for Scaffold Engine."""

import os
import secrets

from fastapi import Request, Security, HTTPException, status
from fastapi.security import APIKeyHeader

_API_KEY = os.environ.get("SCAFFOLD_API_KEY", "")

api_key_header = APIKeyHeader(
    name="X-API-Key",
    auto_error=False,
)


async def require_api_key(
    request: Request,
    key: str | None = Security(api_key_header),
) -> str:
    """Validate X-API-Key header. Returns the key on success, raises 401 on failure."""
    # Let health checks through without auth
    if request.url.path == "/health":
        return ""
    if not _API_KEY:
        # No key configured — auth disabled (dev fallback)
        return ""
    if key is None or not secrets.compare_digest(key, _API_KEY):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing API key",
        )
    return key
