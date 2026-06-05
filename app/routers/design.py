"""
``design_circuit`` job-type HTTP surface (§17.151).

Three endpoints under ``/design``:

  POST   /design                       — extract + create job (or
                                          return ambiguities inline)
  POST   /design/{job_id}/advance      — SSE-streaming per-stage
                                          advance (``?stage=topology``
                                          / ``size`` / ``verify`` / ``report``)
  GET    /design/{job_id}              — aggregated pipeline state

All routes inherit the global ``Depends(require_api_key)`` mounted on
the app (same pattern as the assist / specs routers).

The /confirm gate from §17.145 remains the only way to move a spec
from extracted to operator-confirmed; this router does NOT auto-
confirm. Operators run ``POST /design`` → ``POST /specs/{id}/confirm``
→ ``POST /design/{job_id}/advance?stage=topology`` → ... → ``stage=report``.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.responses import StreamingResponse

from app.database import get_db
from app.schemas import (
    DesignAmbiguityRead,
    DesignCreateInput,
    DesignCreateResponse,
    DesignStateRead,
)
from app.sim.design_pipeline import (
    DesignJobNotFoundError,
    VALID_STAGES,
    advance_design_stage,
    create_design_job,
    get_design_state,
)

router = APIRouter(tags=["Design"], prefix="/design")


@router.post("", response_model=DesignCreateResponse)
async def post_create(
    body: DesignCreateInput,
    db: AsyncSession = Depends(get_db),
) -> DesignCreateResponse:
    """Create a design_circuit job from a natural-language brief.

    Three response shapes, distinguished by which fields are populated:

      * Success — ``job_id`` + ``spec_id`` set; ``ambiguities=[]``
        and ``errors=[]``. Status code 200.
      * Ambiguity — ``ambiguities`` non-empty; ``job_id`` is null and
        no DB rows are persisted. Operator clarifies and re-posts.
        Status code 200.
      * Extractor error — ``errors`` non-empty; no rows persisted.
        Status code 200 (errors are data, not exceptions — same
        posture as the simulator wrappers).
    """
    result = await create_design_job(
        body.brief, db=db, model_role=body.model_role,
    )
    return DesignCreateResponse(
        job_id=result.job_id,
        spec_id=result.spec_id,
        ambiguities=[
            DesignAmbiguityRead(
                field=a.field, reason=a.reason, question=a.question,
            )
            for a in result.ambiguities
        ],
        errors=list(result.errors),
        model_used=result.model_used,
    )


@router.post("/{job_id}/advance")
async def post_advance(
    job_id: uuid.UUID,
    stage: str = "topology",
    db: AsyncSession = Depends(get_db),
):
    """Advance the design_circuit pipeline by one stage. Returns an
    SSE stream — clients should read ``text/event-stream`` events of
    type ``stage_start`` / ``stage_done`` / ``stage_error`` / ``done``.

    Stage semantics:
      * ``topology`` — requires confirmed spec; runs §17.146.
      * ``size``     — requires prior topology selection; runs §17.147.
      * ``verify``   — requires a prior *converged* digital sizing; runs the
                       §17.414 symbiyosys formal-verify loop (digital-only).
      * ``report``   — requires prior device sizing; renders §17.148.

    Per-stage granularity is intentional (per §17.151 design choice) —
    the operator can inspect persisted audit rows between stages and
    correct course without re-running the entire chain.

    Status mapping at the HTTP layer:
      * 400 — unknown stage
      * 404 — job_id not a design_circuit row
      * 200 + SSE stream — every successful invocation; stage outcomes
              live inside the event payloads, not the HTTP code.
    """
    if stage not in VALID_STAGES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"unknown stage {stage!r}; valid: "
                f"{sorted(VALID_STAGES)}"
            ),
        )
    # Validate job_id existence up front so a bad URL returns 404
    # before the SSE connection opens. The advancer's own checks
    # would surface the same error inside an SSE event, but operators
    # debugging 404 vs. legitimate stage failure want the distinction.
    try:
        await get_design_state(job_id, db=db)
    except DesignJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    return StreamingResponse(
        advance_design_stage(job_id, stage, db=db),
        media_type="text/event-stream",
        headers={"X-Accel-Buffering": "no"},
    )


@router.get("/{job_id}", response_model=DesignStateRead)
async def get_state(
    job_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> DesignStateRead:
    """Read the aggregated pipeline state for a design_circuit job.

    A single read that joins jobs ⨝ specs ⨝ topology_selections ⨝
    device_sizings. Nullable fields reflect the furthest-completed
    stage; an operator can poll this to see "is my spec confirmed
    yet?" or "did the sizing converge?" without hitting each stage's
    individual GET surface.

    404 when the job_id is missing or not a design_circuit job.
    """
    try:
        state = await get_design_state(job_id, db=db)
    except DesignJobNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return DesignStateRead(
        job_id=state.job_id,
        job_type=state.job_type,
        status=state.status,
        brief=state.brief,
        created_at=state.created_at,
        spec_id=state.spec_id,
        spec_confirmed_at=state.spec_confirmed_at,
        topology_selection_id=state.topology_selection_id,
        device_sizing_id=state.device_sizing_id,
        device_sizing_converged=state.device_sizing_converged,
    )
