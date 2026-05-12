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
from fastapi.responses import PlainTextResponse

from app.schemas import (
    DeviceSizingRead,
    ReportCitationRead,
    ReportConstraintRead,
    ReportRead,
    ReportSimRunRead,
    SpecPendingListResponse,
    SpecRead,
    TopologyCandidateRead,
    TopologySelectionRead,
)
from app.sim.device_sizing import (
    CandidateIndexError,
    TopologySelectionNotFoundError,
    size_device,
)
from app.sim.report import (
    ReportNotAvailableError,
    build_report,
    render_markdown,
)
from app.sim.spec_store import (
    SpecNotFoundError,
    confirm_spec,
    list_pending_confirmations,
    unconfirm_spec,
)
from app.sim.topology_select import select_topologies

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


@router.post(
    "/{spec_id}/topology-select",
    response_model=TopologySelectionRead,
)
async def post_topology_select(
    spec_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
) -> TopologySelectionRead:
    """Run the topology-selection stage against a confirmed spec.

    The stage retrieves engineering-domain reference chunks, asks the
    configured model to propose 2–4 candidate topologies with
    citations into the retrieval set, hard-rejects any hallucinated
    citation, and persists one ``topology_selections`` row on success.

    Status mapping:
      * 200 — selection persisted; response carries the candidates +
              retrieval audit columns.
      * 404 — spec_id has no row in ``specs``.
      * 409 — spec exists but is not confirmed (or any other
              ``ok=False`` path: RAG empty, LLM failure, hallucinated
              citation, etc.). The body carries ``errors`` so the
              caller can surface the specific reason.
    """
    try:
        result = await select_topologies(spec_id, db=db)
    except SpecNotFoundError:
        raise HTTPException(status_code=404, detail=f"spec {spec_id} not found")

    if not result.ok:
        raise HTTPException(
            status_code=409,
            detail={
                "errors": result.errors,
                "rag_chunk_ids": result.rag_chunk_ids,
                "rag_query": result.rag_query,
            },
        )

    assert result.selection_id is not None
    return TopologySelectionRead(
        id=result.selection_id,
        spec_id=result.spec_id,  # type: ignore[arg-type]
        candidates=[
            TopologyCandidateRead(
                name=c.name,
                description=c.description,
                rationale=c.rationale,
                citations=list(c.citations),
            )
            for c in result.candidates
        ],
        rag_chunk_ids=list(result.rag_chunk_ids),
        rag_query=result.rag_query,
        rag_domain=result.rag_domain,
        model_used=result.model_used,
        # The router doesn't have created_at on the result; fetch from
        # the freshly-inserted row. Cheap single-row lookup; keeps the
        # stage module response-shape-agnostic.
        created_at=await _fetch_created_at(db, result.selection_id),
    )


async def _fetch_created_at(db: AsyncSession, selection_id: uuid.UUID):
    from sqlalchemy import text as _text  # local to avoid leaking
    row = await db.execute(
        _text("SELECT created_at FROM topology_selections WHERE id = :id"),
        {"id": str(selection_id)},
    )
    return row.scalar_one()


# §17.147 — Device-sizing stage. Mounted under /topology-selections/
# because the unit of work is a chosen (spec, topology_candidate)
# pair, not a bare spec.

sizing_router = APIRouter(tags=["Specs"], prefix="/topology-selections")


@sizing_router.post(
    "/{topology_selection_id}/size",
    response_model=DeviceSizingRead,
)
async def post_size_device(
    topology_selection_id: uuid.UUID,
    candidate_idx: int = 0,
    max_iterations: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> DeviceSizingRead:
    """Run the closed-loop sizing stage against a topology candidate.

    Always persists a ``device_sizings`` row when the stage gets past
    the gate checks (confirmed spec, analog_circuit kind, valid
    candidate_idx). The persisted row records the attempt — a
    non-converged sizing carries ``converged=False`` and an
    ``errors`` list explaining why; the wider pipeline accepts it
    as ready only when ``converged=True``.

    Status mapping:
      * 200 — row persisted (caller checks ``converged`` for outcome).
      * 400 — candidate_idx out of bounds for this selection.
      * 404 — topology_selection_id not found.
    """
    try:
        result = await size_device(
            topology_selection_id,
            db=db,
            candidate_idx=candidate_idx,
            max_iterations=max_iterations,
        )
    except TopologySelectionNotFoundError:
        raise HTTPException(
            status_code=404,
            detail=f"topology_selection {topology_selection_id} not found",
        )
    except CandidateIndexError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    assert result.sizing_id is not None
    created_at = await _fetch_sizing_created_at(db, result.sizing_id)

    return DeviceSizingRead(
        id=result.sizing_id,
        spec_id=result.spec_id,  # type: ignore[arg-type]
        topology_selection_id=result.topology_selection_id,  # type: ignore[arg-type]
        candidate_idx=result.candidate_idx,
        converged=result.converged,
        iterations=result.iterations,
        final_params=result.final_params,
        final_netlist=result.final_netlist,
        final_measurements=result.final_measurements,
        sim_run_ids=result.sim_run_ids,
        model_used=result.model_used,
        errors=result.errors,
        created_at=created_at,
    )


async def _fetch_sizing_created_at(db: AsyncSession, sizing_id: uuid.UUID):
    from sqlalchemy import text as _text
    row = await db.execute(
        _text("SELECT created_at FROM device_sizings WHERE id = :id"),
        {"id": str(sizing_id)},
    )
    return row.scalar_one()


# §17.148 — Report stage. GET-only; pure read-side projection of the
# audit tables. ``?format=markdown`` returns text/markdown; the
# default is structured JSON.

report_router = APIRouter(tags=["Specs"], prefix="/device-sizings")


@report_router.get(
    "/{sizing_id}/report",
    responses={
        200: {
            "content": {
                "application/json": {},
                "text/markdown": {},
            }
        }
    },
)
async def get_report(
    sizing_id: uuid.UUID,
    format: str = "json",
    db: AsyncSession = Depends(get_db),
):
    """Render the terminal report for a sizing attempt.

    Non-converged sizings are renderable (with a banner) — the report
    is the post-mortem artefact for "why did this attempt fail?",
    not just the success surface.

    Status:
      * 200 — report rendered.
      * 400 — unknown ``format`` (only ``json`` / ``markdown``).
      * 404 — sizing_id (or any of the rows it references) not found.
    """
    fmt = format.lower()
    if fmt not in ("json", "markdown", "md"):
        raise HTTPException(
            status_code=400,
            detail=f"unsupported format {format!r}; use 'json' or 'markdown'",
        )
    try:
        doc = await build_report(sizing_id, db=db)
    except ReportNotAvailableError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    if fmt in ("markdown", "md"):
        return PlainTextResponse(
            content=render_markdown(doc),
            media_type="text/markdown",
        )

    return ReportRead(
        report_schema_version=doc.report_schema_version,
        generated_at=doc.generated_at,
        sizing_id=doc.sizing_id,
        spec_id=doc.spec_id,
        topology_selection_id=doc.topology_selection_id,
        candidate_idx=doc.candidate_idx,
        converged=doc.converged,
        iterations=doc.iterations,
        design_name=doc.design_name,
        design_kind=doc.design_kind,
        design_description=doc.design_description,
        spec_schema_version=doc.spec_schema_version,
        constraints=[
            ReportConstraintRead(
                id=c.id, kind=c.kind, description=c.description,
                target=c.target, min=c.min, max=c.max,
                tolerance_pct=c.tolerance_pct, unit=c.unit,
                criticality=c.criticality, measured=c.measured,
                status=c.status,
            )
            for c in doc.constraints
        ],
        interfaces=list(doc.interfaces),
        environment=dict(doc.environment),
        selected_topology=dict(doc.selected_topology),
        citations=[
            ReportCitationRead(
                entry_id=cite.entry_id, title=cite.title,
                snippet=cite.snippet, source_url=cite.source_url,
                available=cite.available,
            )
            for cite in doc.citations
        ],
        final_params=dict(doc.final_params),
        final_netlist=doc.final_netlist,
        final_measurements=dict(doc.final_measurements),
        sim_runs=[
            ReportSimRunRead(
                sim_run_id=r.sim_run_id, iteration=r.iteration,
                tool=r.tool, tool_version=r.tool_version,
                exit_code=r.exit_code, timed_out=r.timed_out,
                duration_ms=r.duration_ms,
                measurements=dict(r.measurements),
                verdict=r.verdict,
            )
            for r in doc.sim_runs
        ],
        errors=list(doc.errors),
        model_used=doc.model_used,
    )
