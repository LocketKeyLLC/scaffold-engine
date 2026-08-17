"""§17.809 — runtime compute profiles: activate / clear / inspect surface.

Mounted in ``app/main.py``; inherits the global ``Depends(require_api_key)``.

  GET    /config/profile   — active profile (or null) + the available registry
  POST   /config/profile   — activate {"name": "quick"} globally (persistent)
  DELETE /config/profile   — turn the active profile off (revert every change)

Activation persists (models via the model_overrides table, knobs via the
runtime_profile singleton) and survives restart — see
:mod:`app.modules.profiles`. This router is the thin HTTP gate; all state
transitions live in that module.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import ProfileApplyInput
from app.modules import profiles as _profiles

logger = logging.getLogger("scaffold.profiles")

router = APIRouter(tags=["Config"])


@router.get("/config/profile")
async def get_profile_endpoint(db: AsyncSession = Depends(get_db)) -> dict:
    """The globally-active profile (or ``null``) plus the available registry."""
    active = await _profiles.active_profile(db)
    return {"active": active, "available": _profiles.list_profiles()}


@router.post("/config/profile")
async def apply_profile_endpoint(
    body: ProfileApplyInput, db: AsyncSession = Depends(get_db),
) -> dict:
    """Activate a named profile globally. 404 on an unknown profile name."""
    try:
        result = await _profiles.apply_profile(body.name, db)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return result


@router.delete("/config/profile")
async def clear_profile_endpoint(db: AsyncSession = Depends(get_db)) -> dict:
    """Turn the active profile off, reverting every model + knob it set."""
    return await _profiles.clear_profile(db)
