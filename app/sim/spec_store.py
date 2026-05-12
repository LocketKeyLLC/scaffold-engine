"""
Spec persistence + confirmation gate.

§17.143 added the schema + the ``specs`` table. §17.144 added the
NL→spec extractor that INSERTs rows. §17.145 closes the gate: rows
sit with ``confirmed_*=NULL`` until an operator hits the /confirm
endpoint, and every downstream stage in the engineering-design
pipeline asserts the spec it's about to consume has been confirmed
before doing anything.

Helpers in this module are the only sanctioned way to flip
``confirmed_by`` / ``confirmed_at``. The pipeline stages never write
those columns themselves — they only ever check.

Error contract:

  * Lookup-style helpers (``is_spec_confirmed``,
    ``list_pending_confirmations``) raise ``SpecNotFoundError`` only
    when the spec_id explicitly does not exist; missing-confirmation
    is *not* an error for them.
  * ``require_confirmed_spec`` is the strict gate — raises
    ``SpecNotFoundError`` or ``SpecNotConfirmedError``. Designed to
    be called at the start of every downstream stage so the failure
    mode is "loud, with a specific reason."
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("scaffold")


class SpecNotFoundError(LookupError):
    """Raised when a spec_id has no row in ``specs``."""


class SpecNotConfirmedError(RuntimeError):
    """Raised by ``require_confirmed_spec`` when a spec exists but its
    ``confirmed_*`` columns are NULL. Carries the spec_id so the
    caller can render a clean error / clarifying question."""

    def __init__(self, spec_id: uuid.UUID):
        super().__init__(f"spec {spec_id} is not confirmed")
        self.spec_id = spec_id


@dataclass(frozen=True)
class SpecRow:
    """Lightweight view over a specs row. Pydantic models for HTTP
    responses live in ``app/schemas.py``; this is the internal shape
    that helpers return to other Python code so we don't drag the
    Pydantic dependency into every call site."""
    id: uuid.UUID
    job_id: uuid.UUID | None
    schema_version: str
    spec_json: dict[str, Any]
    spec_sha256: str
    confirmed_by: str | None
    confirmed_at: datetime | None
    created_at: datetime

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed_by is not None and self.confirmed_at is not None

    @property
    def design_name(self) -> str:
        return str(self.spec_json.get("design", {}).get("name", ""))


def _row_to_spec(row: Any) -> SpecRow:
    return SpecRow(
        id=row["id"],
        job_id=row["job_id"],
        schema_version=row["schema_version"],
        spec_json=row["spec_json"],
        spec_sha256=row["spec_sha256"],
        confirmed_by=row["confirmed_by"],
        confirmed_at=row["confirmed_at"],
        created_at=row["created_at"],
    )


async def get_spec(db: AsyncSession, spec_id: uuid.UUID) -> SpecRow:
    """Fetch a single spec row. Raises ``SpecNotFoundError`` if absent."""
    result = await db.execute(
        text(
            """
            SELECT id, job_id, schema_version, spec_json, spec_sha256,
                   confirmed_by, confirmed_at, created_at
            FROM specs
            WHERE id = :id
            """
        ),
        {"id": str(spec_id)},
    )
    row = result.mappings().first()
    if row is None:
        raise SpecNotFoundError(f"spec {spec_id} not found")
    return _row_to_spec(row)


async def confirm_spec(
    db: AsyncSession,
    spec_id: uuid.UUID,
    *,
    confirmed_by: str,
) -> SpecRow:
    """Mark a spec as confirmed.

    Re-confirmation is allowed (user's §17.145 choice): calling this
    on an already-confirmed spec just refreshes ``confirmed_at`` to
    NOW() and updates ``confirmed_by`` to the latest caller. The audit
    of who-confirmed-when over time lives in the LLM call log only
    when the future ``/audit`` surface lands; for v1 the column stores
    the most recent confirmer.

    Raises ``SpecNotFoundError`` if the spec_id is unknown.
    """
    result = await db.execute(
        text(
            """
            UPDATE specs
            SET confirmed_by = :confirmed_by,
                confirmed_at = NOW()
            WHERE id = :id
            RETURNING id, job_id, schema_version, spec_json, spec_sha256,
                      confirmed_by, confirmed_at, created_at
            """
        ),
        {"id": str(spec_id), "confirmed_by": confirmed_by},
    )
    row = result.mappings().first()
    if row is None:
        raise SpecNotFoundError(f"spec {spec_id} not found")
    await db.commit()
    return _row_to_spec(row)


async def unconfirm_spec(
    db: AsyncSession,
    spec_id: uuid.UUID,
) -> SpecRow:
    """Clear a spec's confirmation columns. Idempotent — calling on
    an already-unconfirmed spec is a no-op but still returns the row.

    Raises ``SpecNotFoundError`` if the spec_id is unknown.
    """
    result = await db.execute(
        text(
            """
            UPDATE specs
            SET confirmed_by = NULL,
                confirmed_at = NULL
            WHERE id = :id
            RETURNING id, job_id, schema_version, spec_json, spec_sha256,
                      confirmed_by, confirmed_at, created_at
            """
        ),
        {"id": str(spec_id)},
    )
    row = result.mappings().first()
    if row is None:
        raise SpecNotFoundError(f"spec {spec_id} not found")
    await db.commit()
    return _row_to_spec(row)


async def is_spec_confirmed(db: AsyncSession, spec_id: uuid.UUID) -> bool:
    """True iff the spec exists AND both confirmation columns are non-NULL.

    Distinct from ``require_confirmed_spec``: this is a quiet probe
    (returns False on missing or unconfirmed). Use it for "should I
    show the confirm button" UI decisions. Use ``require_*`` to gate
    downstream stages.
    """
    result = await db.execute(
        text(
            """
            SELECT (confirmed_by IS NOT NULL
                    AND confirmed_at IS NOT NULL) AS is_confirmed
            FROM specs
            WHERE id = :id
            """
        ),
        {"id": str(spec_id)},
    )
    row = result.scalar_one_or_none()
    return bool(row) if row is not None else False


async def require_confirmed_spec(
    db: AsyncSession,
    spec_id: uuid.UUID,
) -> SpecRow:
    """Strict gate for downstream pipeline stages.

    Returns the spec row when confirmed. Raises
    ``SpecNotFoundError`` if absent, ``SpecNotConfirmedError`` if
    present-but-unconfirmed. Designed to be the first line of every
    stage that depends on a confirmed spec — the exception type tells
    the caller exactly which failure mode to surface.
    """
    spec = await get_spec(db, spec_id)
    if not spec.is_confirmed:
        raise SpecNotConfirmedError(spec_id)
    return spec


async def list_pending_confirmations(
    db: AsyncSession,
    *,
    job_id: uuid.UUID | None = None,
    limit: int = 100,
) -> list[SpecRow]:
    """List specs awaiting confirmation, oldest first.

    Filters to a specific job_id when provided, else returns all
    pending across the deployment. The ``idx_specs_confirmed_at``
    partial index (created in migration 040) doesn't accelerate
    *unconfirmed* lookups directly, but the typical pending list is
    small and the planner just sequential-scans the small set.
    """
    if job_id is None:
        result = await db.execute(
            text(
                """
                SELECT id, job_id, schema_version, spec_json, spec_sha256,
                       confirmed_by, confirmed_at, created_at
                FROM specs
                WHERE confirmed_at IS NULL
                ORDER BY created_at ASC
                LIMIT :lim
                """
            ),
            {"lim": limit},
        )
    else:
        result = await db.execute(
            text(
                """
                SELECT id, job_id, schema_version, spec_json, spec_sha256,
                       confirmed_by, confirmed_at, created_at
                FROM specs
                WHERE confirmed_at IS NULL AND job_id = :job_id
                ORDER BY created_at ASC
                LIMIT :lim
                """
            ),
            {"job_id": str(job_id), "lim": limit},
        )
    return [_row_to_spec(r) for r in result.mappings().all()]
