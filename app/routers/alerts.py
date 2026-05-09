"""Sprint X.26 — read-only alerts surface.

Mounted in `app/main.py`; inherits global `Depends(require_api_key)`.

  GET /observability/alerts       — recent system_alerts rows

Emit happens via the alerts CLI (calibration cron) and the threshold
scheduler tick — there's deliberately no POST surface here. Letting
arbitrary HTTP callers create alerts would muddle the audit trail and
invite OWUI users to fire critical pages by accident.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.observability import alerts as _alerts

router = APIRouter(tags=["Observability"])


@router.get("/observability/alerts")
async def list_alerts_endpoint(
    kind: str | None = Query(None, description="Filter by alert kind (exact match)."),
    since_minutes: int | None = Query(
        None, ge=1, le=10080,
        description="Only alerts created within the last N minutes.",
    ),
    limit: int = Query(100, ge=1, le=500, description="Max rows returned."),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Recent alerts. Sorted newest first. Fail-open (empty list) on DB error."""
    return await _alerts.list_recent(
        kind=kind, since_minutes=since_minutes, limit=limit, db=db,
    )
