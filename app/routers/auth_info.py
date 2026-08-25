"""§17.815 — caller identity for the SPA login flow (plan 5.3).

Mounted in ``app/main.py``; inherits the global ``Depends(require_api_key)``,
so reaching it at all proves the key is valid — the endpoint just reports who
that key is. The SPA calls this after the operator pastes a key to show
"signed in as X (role)" and to gate admin-only navigation client-side (the
server still enforces authz on every route — this is display, not security).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.authz import Principal, get_principal
from app.config import settings

router = APIRouter(tags=["Auth"])


@router.get("/auth/whoami")
async def whoami(principal: Principal = Depends(get_principal)) -> dict:
    """The authenticated caller's identity, role, and the auth mode in force."""
    return {
        "identity": principal.identity,
        "role": principal.role,
        "is_admin": principal.is_admin,
        "key_id": principal.key_id,
        "multi_user": bool(settings.multi_user_enabled),
    }
