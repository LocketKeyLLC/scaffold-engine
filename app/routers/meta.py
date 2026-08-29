"""§17.817 — first-run state for the connect-models wizard (plan 5.7).

Mounted in ``app/main.py``; inherits the global ``Depends(require_api_key)``.

  GET  /meta/first-run           — should the SPA route to the setup wizard?
  POST /meta/first-run/complete  — the wizard finished; never show it again.

Server-side (system_flags, mig 070), not localStorage: per-browser first-run
state re-triggered the welcome flow on every new device (audit LOW).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.authz import require_admin
from app.database import get_db

router = APIRouter(tags=["Meta"])

_FLAG = "first_run_completed"

# §17.818 (plan 5.8) — the user-selectable ingest/build domains. Single source
# for every client picker (SPA compose/rag/schedules/library) — the audit
# found the list duplicated as constants in four SPA views + the retired /web
# template. The user-facing six; VALID_DOMAINS' code/qa are internal-only
# partitions.
_USER_DOMAINS = ("prompt", "rag", "llm", "spec", "eng", "eng_design")


@router.get("/meta/first-run")
async def get_first_run(db: AsyncSession = Depends(get_db)) -> dict:
    """Whether this install still needs the connect-models wizard.

    The explicit flag wins. When it has never been written (fresh install OR
    an existing box upgrading onto this feature), fall back to a heuristic:
    an install with jobs or persisted model overrides has clearly been used —
    never nag it with a first-run wizard. Only a genuinely empty engine
    (no jobs, no overrides) reads as first-run."""
    row = (await db.execute(
        text("SELECT value FROM system_flags WHERE key = :k"), {"k": _FLAG},
    )).scalar()
    if row is not None:
        completed = bool(row.get("completed")) if isinstance(row, dict) else bool(row)
        return {"first_run": not completed, "source": "flag"}
    jobs = (await db.execute(text("SELECT COUNT(*) FROM jobs"))).scalar() or 0
    overrides = (await db.execute(
        text("SELECT COUNT(*) FROM model_overrides"))).scalar() or 0
    return {"first_run": jobs == 0 and overrides == 0, "source": "heuristic"}


@router.post("/meta/first-run/complete", dependencies=[Depends(require_admin)])
async def complete_first_run(db: AsyncSession = Depends(get_db)) -> dict:
    """Mark the wizard done (idempotent)."""
    await db.execute(
        text("""
            INSERT INTO system_flags (key, value, updated_at)
            VALUES (:k, CAST(:v AS jsonb), now())
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
        """),
        {"k": _FLAG, "v": '{"completed": true}'},
    )
    await db.commit()
    return {"first_run": False, "source": "flag"}


@router.get("/meta/domains")
async def get_domains() -> dict:
    """The user-selectable domain list for client pickers.

    §17.820 — derived from ``idea_refinement.ALLOWED_DOMAINS`` (the set
    ``create_ideation_job`` + IdeaInput actually enforce) instead of a
    hardcoded twin; _USER_DOMAINS pins the presentation order."""
    from app.modules.idea_refinement import ALLOWED_DOMAINS

    return {"domains": [d for d in _USER_DOMAINS if d in ALLOWED_DOMAINS]}
