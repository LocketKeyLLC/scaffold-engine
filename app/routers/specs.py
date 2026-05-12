"""
Spec confirmation gate routes (§17.145).

Three endpoints scoped under /specs/:

  POST   /specs/{spec_id}/confirm    — set confirmed_by + confirmed_at = NOW()
  POST   /specs/{spec_id}/unconfirm  — clear confirmed_by + confirmed_at
  GET    /specs/pending              — list specs awaiting confirmation

All routes inherit the global ``Depends(require_api_key)`` mounted on
the app (mirrors the assist router pattern). ``confirmed_by`` is
recorded as the literal string ``"api_key"`` since the orchestrator's
SCAFFOLD_API_KEY auth is anonymous — a future commit can plug in
proper operator identity (e.g. X-User header or token subject) and
backfill the column without a migration.

Why a dedicated endpoint instead of overloading /ideate/confirm: the
ideation /confirm advances a job from ``awaiting_confirmation`` to
``researching``, which is unrelated to whether a separately-extracted
spec has been operator-acknowledged. Coupling them would mean
``/confirm`` does different things depending on hidden state, which
is exactly the surprise the §17.144 strict-envelope design is meant
to avoid.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.schemas import SpecPendingListResponse, SpecRead
from app.sim.spec_store import (
    SpecNotFoundError,
    confirm_spec,
    list_pending_confirmations,
    unconfirm_spec,
)

router = APIRouter(tags=["Specs"], prefix="/specs")

# Source of truth for confirmed_by when the auth context is the
# anonymous API key. Hoisted so the integration tests can assert
# against it directly rather than re-deriving the string.
CONFIRMED_BY_API_KEY = "api_key"


def _to_read(spec_row) -> SpecRead:
    return SpecRead(
        id=spec_row.id,
        job_id=spec_row.job_id,
        schema_version=spec_row.schema_version,
        spec_json=spec_row.spec_json,
        spec_sha256=spec_row.spec_sha256,
        confirmed_by=spec_row.confirmed_by,
        confirmed_at=spec_row.confirmed_at,
        created_at=spec_row.created_at,
    )


@router.post("/{spec_id}/confirm", response_model=SpecRead)
async def post_confirm(
    spec_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SpecRead:
    """Mark a spec as operator-confirmed.

    Idempotent — re-confirming an already-confirmed spec just
    refreshes ``confirmed_at`` and rewrites ``confirmed_by``. The
    audit of who-confirmed-when over time is deferred to the future
    audit surface; the column carries only the most recent confirmer.
    """
    try:
        row = await confirm_spec(db, spec_id, confirmed_by=CONFIRMED_BY_API_KEY)
    except SpecNotFoundError:
        raise HTTPException(status_code=404, detail=f"spec {spec_id} not found")
    return _to_read(row)


@router.post("/{spec_id}/unconfirm", response_model=SpecRead)
async def post_unconfirm(
    spec_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> SpecRead:
    """Clear a spec's confirmation columns. Use when a confirmed spec
    needs to be revoked (e.g. a downstream stage flagged a problem and
    the operator wants to extract a fresh spec).

    Idempotent — calling on an already-unconfirmed spec is a no-op.
    """
    try:
        row = await unconfirm_spec(db, spec_id)
    except SpecNotFoundError:
        raise HTTPException(status_code=404, detail=f"spec {spec_id} not found")
    return _to_read(row)


@router.get("/pending", response_model=SpecPendingListResponse)
async def get_pending(
    job_id: uuid.UUID | None = None,
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
) -> SpecPendingListResponse:
    """List specs awaiting operator confirmation, oldest first.

    Optional ``job_id`` query parameter scopes the list to a specific
    job; without it the list is global across the deployment. UI
    layers use this to render a "needs your attention" panel.
    """
    rows = await list_pending_confirmations(db, job_id=job_id, limit=limit)
    items = [_to_read(r) for r in rows]
    return SpecPendingListResponse(pending=items, count=len(items))
