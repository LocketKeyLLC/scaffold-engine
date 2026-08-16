"""§17.803 — role→model swap proposals: review + apply surface.

Mounted in ``app/main.py``; inherits the global ``Depends(require_api_key)``.

  GET  /models/proposals            — open (staged) proposals, newest first
  POST /models/proposals/{id}/accept  — apply the swap via set_override
  POST /models/proposals/{id}/dismiss — drop the proposal (no override written)

Proposals are STAGED by the periodic learning job (app/modules/model_role_
learning). This router is the human gate — the OWUI ``/model proposals`` +
confirm-card flow calls these endpoints. Nothing here runs the A/B; it only
reads staged rows and applies an operator-confirmed decision.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.modules import model_role_learning as _mrl

router = APIRouter(tags=["Models"])


@router.get("/models/proposals")
async def list_proposals_endpoint(db: AsyncSession = Depends(get_db)) -> dict:
    """Open role→model swap proposals awaiting review, newest first."""
    proposals = await _mrl.list_open_proposals(db)
    return {"proposals": proposals, "count": len(proposals)}


@router.post("/models/proposals/{proposal_id}/accept")
async def accept_proposal_endpoint(
    proposal_id: int, db: AsyncSession = Depends(get_db),
) -> dict:
    """Apply a staged swap (set_override) and mark the proposal accepted.

    404 if the proposal is missing or no longer open (already accepted/
    dismissed/superseded) — a stale confirm card must not silently re-apply.
    """
    result = await _mrl.accept_proposal(proposal_id, db)
    if result is None:
        raise HTTPException(status_code=404, detail="proposal not found or not open")
    return result


@router.post("/models/proposals/{proposal_id}/dismiss")
async def dismiss_proposal_endpoint(
    proposal_id: int, db: AsyncSession = Depends(get_db),
) -> dict:
    """Dismiss a staged proposal without applying it."""
    result = await _mrl.dismiss_proposal(proposal_id, db)
    if result is None:
        raise HTTPException(status_code=404, detail="proposal not found or not open")
    return result
