"""
Design-circuit job-type orchestrator (§17.151).

Glues the four engineering-design stages (extract → confirm → topology-
select → size → report) into a single ``job_type='design_circuit'``
lifecycle on the existing jobs table.

Two public-API surfaces:

  * ``create_design_job(brief, *, db)`` — one-shot creation. Runs the
    §17.144 extractor; on success creates a ``jobs`` row with
    ``job_type='design_circuit'`` and backfills ``specs.job_id``.
    On ambiguity or extractor error, returns the structured result
    inline and does NOT persist a job row (per the §17.151 design
    choice — avoids stranded jobs from a single missing number).

  * ``advance_design_stage(job_id, stage, *, db)`` — async generator
    that drives a single stage of the downstream chain and yields
    SSE event strings. Stages are per-call (``stage="topology"``,
    ``"size"``, or ``"report"``), so an operator can drive the
    pipeline one step at a time and inspect the audit row between
    invocations.

Plus a read-side aggregator:

  * ``get_design_state(job_id, *, db)`` — joins jobs + specs +
    topology_selections + device_sizings for a single design_circuit
    job and returns the cross-stage state in one read.

The job's ``status`` column moves through the existing 14-value set:

  pending → refining → awaiting_confirmation → planning → executing
   → completed | failed

Each per-stage advancer is responsible for the transition before the
stage runs (e.g. ``awaiting_confirmation → planning`` when topology
starts) and after (``planning → executing`` on topology success;
``planning → failed`` on terminal stage error).
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.sim.device_sizing import (
    CandidateIndexError,
    TopologySelectionNotFoundError,
    size_device,
)
from app.sim.digital_sizing import size_digital_device
from app.sim.formal_verify import (
    DigitalSizingNotFoundError,
    verify_design,
)
from app.sim.report import (
    ReportDocument,
    ReportNotAvailableError,
    build_report,
    render_markdown,
)
from app.sim.spec_extractor import ExtractionAmbiguity, extract_spec
from app.sim.spec_store import (
    SpecNotFoundError,
)
from app.sim.topology_select import select_topologies

logger = logging.getLogger("scaffold")

JOB_TYPE = "design_circuit"
VALID_STAGES = frozenset({"topology", "size", "verify", "report"})


class DesignJobNotFoundError(LookupError):
    """Raised by advance / get_state when the job_id is not a
    ``design_circuit`` job (either missing or wrong type)."""


class DesignBadStageError(ValueError):
    """Raised when the ``stage`` argument is not one of the
    recognised stage names."""


@dataclass
class DesignCreateResult:
    """Result of ``create_design_job`` — exactly one of (job_id +
    spec_id) or (ambiguities) or (errors) carries content. The HTTP
    layer maps these to a single 200 response shape."""
    job_id: uuid.UUID | None = None
    spec_id: uuid.UUID | None = None
    ambiguities: list[ExtractionAmbiguity] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    model_used: str = ""


@dataclass
class DesignState:
    """Aggregated state for a single design_circuit job. Returned by
    ``get_design_state``; nullable fields reflect the pipeline's
    current furthest stage."""
    job_id: uuid.UUID
    job_type: str
    status: str
    brief: str
    created_at: Any
    spec_id: uuid.UUID | None
    spec_confirmed_at: Any
    topology_selection_id: uuid.UUID | None
    device_sizing_id: uuid.UUID | None
    device_sizing_converged: bool | None
    # §17.414 — formal-verify stage surface. NULL until a digital design's
    # verify stage has run; ``formal_verdict`` is the latest sby verdict.
    formal_verification_id: uuid.UUID | None
    formal_verdict: str | None


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _sse(event: str, data: dict[str, Any]) -> str:
    """Format a single SSE event. Mirrors the existing
    ``execute_all_nodes`` formatter so clients with SSE plumbing
    already in place don't need a second parser."""
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _set_job_status(
    db: AsyncSession,
    job_id: uuid.UUID,
    new_status: str,
) -> bool:
    """Atomically set status, refusing to overwrite ``'cancelled'``.

    §17.356 — pre-§17.356 this unconditionally overwrote whatever
    status was in place, so a ``POST /jobs/{id}/cancel`` (§17.322)
    landing mid-stage would be silently clobbered by the stage's
    success/failure write. Operator's cancel intent lost; design
    pipeline ran to completion regardless. The §17.350 entry called
    this out as one of two genuinely open operator-driven items.

    Now uses the same ``status != 'cancelled'`` guard pattern that
    ``cancel_active_job`` uses on the other side of the race, so
    cancel is the strictly-sticky terminal state during a stage.
    Returns True on transition, False if the row was already
    cancelled — caller emits an SSE ``cancelled`` event and stops
    instead of yielding ``stage_done`` against a now-cancelled job.
    """
    result = await db.execute(
        text(
            """
            UPDATE jobs
            SET status = :status, updated_at = NOW()
            WHERE id = :id
              AND status != 'cancelled'
            """
        ),
        {"id": str(job_id), "status": new_status},
    )
    await db.commit()
    return bool(result.rowcount)


async def _job_was_cancelled(
    db: AsyncSession, job_id: uuid.UUID,
) -> bool:
    """§17.356 — quiet probe: did the operator cancel mid-stage?

    Used by stage handlers after a long await (LLM call, ngspice
    iteration loop) to detect whether to emit ``cancelled`` instead
    of the stage's normal outcome. The status SELECT is cheap and
    runs outside any FOR UPDATE lock; race with a concurrent
    /cancel is acceptable — worst case the next status write
    becomes the loser via `_set_job_status`'s WHERE guard.
    """
    row = await db.execute(
        text("SELECT status FROM jobs WHERE id = :id"),
        {"id": str(job_id)},
    )
    r = row.mappings().first()
    return r is not None and r["status"] == "cancelled"


async def _fetch_design_job(
    db: AsyncSession, job_id: uuid.UUID
) -> dict[str, Any]:
    row = await db.execute(
        text(
            """
            SELECT id, status, input_text, created_at, updated_at, job_type
            FROM jobs
            WHERE id = :id
            """
        ),
        {"id": str(job_id)},
    )
    r = row.mappings().first()
    if r is None:
        raise DesignJobNotFoundError(f"job {job_id} not found")
    if r["job_type"] != JOB_TYPE:
        raise DesignJobNotFoundError(
            f"job {job_id} is not a design_circuit job "
            f"(job_type={r['job_type']!r})"
        )
    return dict(r)


async def _fetch_spec_for_job(
    db: AsyncSession, job_id: uuid.UUID
) -> dict[str, Any] | None:
    """Latest spec row bound to this job, or None if extract never
    succeeded. Multiple specs per job are tolerated (operator may
    re-extract after un-confirming); the latest wins."""
    row = await db.execute(
        text(
            """
            SELECT id, confirmed_at
            FROM specs
            WHERE job_id = :id
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"id": str(job_id)},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def _fetch_latest_topology_selection(
    db: AsyncSession, spec_id: uuid.UUID
) -> dict[str, Any] | None:
    row = await db.execute(
        text(
            """
            SELECT id
            FROM topology_selections
            WHERE spec_id = :spec_id
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"spec_id": str(spec_id)},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def _fetch_latest_device_sizing(
    db: AsyncSession, topology_selection_id: uuid.UUID
) -> dict[str, Any] | None:
    row = await db.execute(
        text(
            """
            SELECT id, converged
            FROM device_sizings
            WHERE topology_selection_id = :sid
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"sid": str(topology_selection_id)},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def _fetch_latest_sizing_any_kind(
    db: AsyncSession, topology_selection_id: uuid.UUID
) -> dict[str, Any] | None:
    """§17.153 — pick the most recent sizing attempt across BOTH
    ``device_sizings`` (analog) and ``digital_sizings`` (digital).
    The design pipeline's stage=report needs this so it can render
    either kind from the same advance call."""
    row = await db.execute(
        text(
            """
            SELECT id, converged, 'analog' AS kind, created_at
            FROM device_sizings WHERE topology_selection_id = :sid
            UNION ALL
            SELECT id, converged, 'digital' AS kind, created_at
            FROM digital_sizings WHERE topology_selection_id = :sid
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"sid": str(topology_selection_id)},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def _fetch_latest_converged_digital_sizing(
    db: AsyncSession, topology_selection_id: uuid.UUID
) -> dict[str, Any] | None:
    """§17.414 — the latest *converged* digital sizing for a topology
    selection. The formal-verify stage needs a Verilator-proven DUT as its
    starting point; a non-converged sizing has nothing trustworthy to verify."""
    row = await db.execute(
        text(
            """
            SELECT id, converged
            FROM digital_sizings
            WHERE topology_selection_id = :sid AND converged = TRUE
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"sid": str(topology_selection_id)},
    )
    r = row.mappings().first()
    return dict(r) if r else None


async def _fetch_latest_formal_verification(
    db: AsyncSession, topology_selection_id: uuid.UUID
) -> dict[str, Any] | None:
    """§17.414 — the latest formal-verification attempt for a topology
    selection (surfaced by ``get_design_state``). ``formal_verifications``
    carries ``topology_selection_id`` directly, so no join is needed."""
    row = await db.execute(
        text(
            """
            SELECT id, verdict, converged
            FROM formal_verifications
            WHERE topology_selection_id = :sid
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"sid": str(topology_selection_id)},
    )
    r = row.mappings().first()
    return dict(r) if r else None


# ---------------------------------------------------------------------------
# Public — create
# ---------------------------------------------------------------------------

async def create_design_job(
    brief: str,
    *,
    db: AsyncSession,
    model_role: str | None = None,
) -> DesignCreateResult:
    """Run the §17.144 extractor on ``brief``. On extractor success,
    create a ``jobs`` row with ``job_type='design_circuit'`` in
    ``awaiting_confirmation`` and backfill the spec's ``job_id``.

    On ambiguity or extractor error, return the structured result
    without writing a job row. Per §17.151 design choice — keeps
    failed extractions out of the jobs lifecycle so an operator
    re-trying a brief doesn't accumulate failed jobs.
    """
    if not brief or not brief.strip():
        raise ValueError("brief must be non-empty")

    extraction = await extract_spec(brief, db=db, model_role=model_role)

    if extraction.ambiguities:
        return DesignCreateResult(
            ambiguities=list(extraction.ambiguities),
            model_used=extraction.model_used,
        )
    if extraction.errors or not extraction.ok or extraction.spec_id is None:
        return DesignCreateResult(
            errors=list(extraction.errors) or ["extraction failed"],
            model_used=extraction.model_used,
        )

    # Extraction succeeded — create the job row, link the spec to it.
    title = (extraction.spec or {}).get("design", {}).get("name") or "design"
    job_row = await db.execute(
        text(
            """
            INSERT INTO jobs (
                title, description, status, input_text, job_type
            )
            VALUES (
                :title, :description, 'awaiting_confirmation',
                :input_text, :job_type
            )
            RETURNING id
            """
        ),
        {
            "title": title,
            "description": brief.strip()[:1000],
            "input_text": brief.strip(),
            "job_type": JOB_TYPE,
        },
    )
    job_id = job_row.scalar_one()

    await db.execute(
        text("UPDATE specs SET job_id = :job_id WHERE id = :spec_id"),
        {"job_id": str(job_id), "spec_id": str(extraction.spec_id)},
    )
    await db.commit()

    return DesignCreateResult(
        job_id=job_id,
        spec_id=extraction.spec_id,
        model_used=extraction.model_used,
    )


# ---------------------------------------------------------------------------
# Public — advance (SSE)
# ---------------------------------------------------------------------------

async def advance_design_stage(
    job_id: uuid.UUID,
    stage: str,
    *,
    db: AsyncSession,
) -> AsyncGenerator[str, None]:
    """SSE-streaming advancer. Yields the standard ``event:``/``data:``
    SSE payload format expected by the existing /execute/all clients.

    Events emitted:
      * ``stage_start`` — { stage, job_id }
      * ``stage_done``  — stage-specific payload (selection_id /
                          sizing_id / report)
      * ``stage_error`` — { stage, errors[] }
      * ``done``        — terminal envelope

    Status transitions on success:
      topology: awaiting_confirmation → planning
      size:     planning → executing
      verify:   executing → executing (no transition — §17.414 formal
                attestation; digital-only)
      report:   executing → completed
    On terminal stage failure: → failed.
    """
    if stage not in VALID_STAGES:
        yield _sse(
            "stage_error",
            {
                "stage": stage,
                "errors": [
                    f"unknown stage {stage!r}; valid: "
                    f"{sorted(VALID_STAGES)}"
                ],
            },
        )
        yield _sse("done", {"ok": False})
        return

    try:
        job = await _fetch_design_job(db, job_id)
    except DesignJobNotFoundError as exc:
        yield _sse(
            "stage_error",
            {"stage": stage, "errors": [str(exc)]},
        )
        yield _sse("done", {"ok": False})
        return

    # §17.356 — precondition probe: if the operator already cancelled
    # this job (via POST /jobs/{id}/cancel, §17.322), refuse to advance.
    # Mirrors execute_all_nodes' precondition check; pre-§17.356 the
    # design pipeline had no such guard and would happily transition a
    # cancelled job through topology → planning → executing → completed,
    # silently overwriting the cancel.
    if job.get("status") == "cancelled":
        yield _sse(
            "cancelled",
            {
                "stage": stage,
                "job_id": str(job_id),
                "reason": (
                    "job was cancelled before this stage started — "
                    "refusing to advance. POST /jobs/{id}/resume to lift."
                ),
            },
        )
        yield _sse("done", {"ok": False})
        return

    spec_row = await _fetch_spec_for_job(db, job_id)
    if spec_row is None:
        yield _sse(
            "stage_error",
            {
                "stage": stage,
                "errors": [f"job {job_id} has no extracted spec"],
            },
        )
        yield _sse("done", {"ok": False})
        return

    spec_id: uuid.UUID = spec_row["id"]

    yield _sse("stage_start", {"stage": stage, "job_id": str(job_id)})

    if stage == "topology":
        await _set_job_status(db, job_id, "planning")
        try:
            result = await select_topologies(spec_id, db=db)
        except SpecNotFoundError as exc:
            await _set_job_status(db, job_id, "failed")
            yield _sse(
                "stage_error",
                {"stage": stage, "errors": [str(exc)]},
            )
            yield _sse("done", {"ok": False})
            return
        # §17.356 — post-await cancellation check. select_topologies
        # is LLM-driven and can take 30-90 s; an operator may cancel
        # during that window. Honor it by emitting 'cancelled' and
        # NOT writing stage_done / not flipping status to anything
        # that would clobber the sticky 'cancelled'.
        if await _job_was_cancelled(db, job_id):
            yield _sse(
                "cancelled",
                {"stage": stage, "job_id": str(job_id),
                 "reason": "cancelled during topology selection"},
            )
            yield _sse("done", {"ok": False})
            return
        if not result.ok:
            await _set_job_status(db, job_id, "failed")
            yield _sse(
                "stage_error",
                {"stage": stage, "errors": result.errors},
            )
            yield _sse("done", {"ok": False})
            return
        yield _sse(
            "stage_done",
            {
                "stage": stage,
                "selection_id": str(result.selection_id),
                "candidates": [c.to_dict() for c in result.candidates],
            },
        )
        yield _sse("done", {"ok": True})
        return

    if stage == "size":
        sel = await _fetch_latest_topology_selection(db, spec_id)
        if sel is None:
            yield _sse(
                "stage_error",
                {
                    "stage": stage,
                    "errors": [
                        "no topology_selection for this job — run "
                        "stage=topology first"
                    ],
                },
            )
            yield _sse("done", {"ok": False})
            return
        await _set_job_status(db, job_id, "executing")
        # §17.152 — dispatch on the spec's design.kind. _fetch_spec_for_job
        # only selected id+confirmed_at, so fetch spec_json here for the
        # discriminator. One extra SELECT; the sizing loop that follows
        # dwarfs it.
        spec_full = await db.execute(
            text(
                "SELECT spec_json FROM specs WHERE id = :id"
            ),
            {"id": str(spec_id)},
        )
        spec_full_row = spec_full.mappings().first()
        design_kind = None
        if spec_full_row is not None:
            design_kind = (
                (spec_full_row["spec_json"] or {})
                .get("design", {})
                .get("kind")
            )
        try:
            if design_kind == "digital_logic":
                d_result = await size_digital_device(sel["id"], db=db)
                sizing_dict = {
                    "stage": stage,
                    "kind": "digital",
                    "sizing_id": str(d_result.sizing_id),
                    "converged": d_result.converged,
                    "iterations": d_result.iterations,
                    "errors": d_result.errors,
                }
                converged_flag = d_result.converged
            else:
                # Default to the analog ngspice sizer for
                # 'analog_circuit' or unset/unknown kinds. The sizer's
                # own gate will refuse non-analog kinds with a clear
                # error message.
                a_result = await size_device(sel["id"], db=db)
                sizing_dict = {
                    "stage": stage,
                    "kind": "analog",
                    "sizing_id": str(a_result.sizing_id),
                    "converged": a_result.converged,
                    "iterations": a_result.iterations,
                    "errors": a_result.errors,
                }
                converged_flag = a_result.converged
        except (TopologySelectionNotFoundError, CandidateIndexError) as exc:
            await _set_job_status(db, job_id, "failed")
            yield _sse(
                "stage_error",
                {"stage": stage, "errors": [str(exc)]},
            )
            yield _sse("done", {"ok": False})
            return
        # §17.356 — post-await cancellation check. size_device /
        # size_digital_device runs an LLM+sim iteration loop bounded
        # by device_sizing_max_iterations (default 3) and can take
        # several minutes. Cancel during the loop is honored here.
        if await _job_was_cancelled(db, job_id):
            yield _sse(
                "cancelled",
                {"stage": stage, "job_id": str(job_id),
                 "reason": "cancelled during device sizing"},
            )
            yield _sse("done", {"ok": False})
            return
        # §17.615 (audit #27) — the sizer can return ok=False WITHOUT raising and
        # without persisting (unconfirmed spec, unsupported design.kind): sizing_id
        # is None, which str()'d to the literal "None" above. Emit stage_error
        # (surfacing the sizer's errors) instead of a stage_done that both
        # misreports a non-persisted failure as a completed artifact-producing
        # stage AND hands clients the invalid UUID string "None".
        if sizing_dict.get("sizing_id") == "None":
            await _set_job_status(db, job_id, "failed")
            yield _sse("stage_error", {
                "stage": stage,
                "errors": sizing_dict.get("errors") or ["sizing produced no persisted result"],
            })
            yield _sse("done", {"ok": False})
            return
        yield _sse("stage_done", sizing_dict)
        if not converged_flag:
            # Job stays in ``executing`` — operator can re-run size
            # (perhaps with a different candidate_idx) without first
            # transitioning out of failed.
            yield _sse("done", {"ok": False})
            return
        yield _sse("done", {"ok": True})
        return

    if stage == "verify":
        # §17.414 — formal verification (symbiyosys). Digital-only and runs
        # against a *converged* digital sizing (the Verilator-proven DUT).
        sel = await _fetch_latest_topology_selection(db, spec_id)
        if sel is None:
            yield _sse(
                "stage_error",
                {
                    "stage": stage,
                    "errors": [
                        "no topology_selection for this job — run "
                        "stage=topology first"
                    ],
                },
            )
            yield _sse("done", {"ok": False})
            return
        sizing = await _fetch_latest_converged_digital_sizing(db, sel["id"])
        if sizing is None:
            yield _sse(
                "stage_error",
                {
                    "stage": stage,
                    "errors": [
                        "no converged digital sizing for this job — formal "
                        "verification applies only to design.kind="
                        "'digital_logic'; run stage=size until it converges "
                        "first"
                    ],
                },
            )
            yield _sse("done", {"ok": False})
            return
        # Job stays in ``executing`` (no new status value — verify is a formal
        # attestation step; ``report`` still finalizes to ``completed``).
        try:
            v_result = await verify_design(sizing["id"], db=db)
        except (
            TopologySelectionNotFoundError,
            CandidateIndexError,
            DigitalSizingNotFoundError,
        ) as exc:
            await _set_job_status(db, job_id, "failed")
            yield _sse(
                "stage_error",
                {"stage": stage, "errors": [str(exc)]},
            )
            yield _sse("done", {"ok": False})
            return
        # §17.356 — post-await cancellation check. verify_design runs an
        # LLM+sby repair loop that can take minutes; honor a cancel landing
        # during the window.
        if await _job_was_cancelled(db, job_id):
            yield _sse(
                "cancelled",
                {"stage": stage, "job_id": str(job_id),
                 "reason": "cancelled during formal verification"},
            )
            yield _sse("done", {"ok": False})
            return
        yield _sse(
            "stage_done",
            {
                "stage": stage,
                "formal_verification_id": str(v_result.formal_verification_id),
                "verdict": v_result.verdict,
                "converged": v_result.converged,
                "depth_reached": v_result.depth_reached,
                "iterations": v_result.iterations,
                "errors": v_result.errors,
            },
        )
        # Non-PASS leaves the job in ``executing`` — operator can re-run
        # verify or proceed to report (which renders a non-converged result).
        yield _sse("done", {"ok": v_result.converged})
        return

    if stage == "report":
        sel = await _fetch_latest_topology_selection(db, spec_id)
        if sel is None:
            yield _sse(
                "stage_error",
                {
                    "stage": stage,
                    "errors": [
                        "no topology_selection for this job — run "
                        "stage=topology first"
                    ],
                },
            )
            yield _sse("done", {"ok": False})
            return
        # §17.153 — accept either analog (device_sizings) or digital
        # (digital_sizings) sizing; build_report dispatches internally.
        sizing = await _fetch_latest_sizing_any_kind(db, sel["id"])
        if sizing is None:
            yield _sse(
                "stage_error",
                {
                    "stage": stage,
                    "errors": [
                        "no sizing for this job — run stage=size "
                        "first"
                    ],
                },
            )
            yield _sse("done", {"ok": False})
            return
        try:
            doc: ReportDocument = await build_report(sizing["id"], db=db)
        except ReportNotAvailableError as exc:
            yield _sse(
                "stage_error",
                {"stage": stage, "errors": [str(exc)]},
            )
            yield _sse("done", {"ok": False})
            return
        # §17.356 — post-await cancellation check. build_report is
        # pure-Postgres-and-Milvus and quick (sub-second on this
        # corpus), but a /cancel landing between our last check and
        # the success status write would have been clobbered pre-
        # §17.356. Now `_set_job_status` itself refuses to overwrite
        # 'cancelled', so even if this check misses the race the
        # cancel survives — the explicit check just lets us yield
        # the right SSE event for cleaner client UX.
        if await _job_was_cancelled(db, job_id):
            yield _sse(
                "cancelled",
                {"stage": stage, "job_id": str(job_id),
                 "reason": "cancelled during report build"},
            )
            yield _sse("done", {"ok": False})
            return
        if doc.converged:
            transitioned = await _set_job_status(db, job_id, "completed")
            if not transitioned:
                # §17.356 — _set_job_status refused (cancelled won the
                # race). Emit cancelled instead of stage_done so the
                # client doesn't think we shipped a final report.
                yield _sse(
                    "cancelled",
                    {"stage": stage, "job_id": str(job_id),
                     "reason": "cancelled during final status write"},
                )
                yield _sse("done", {"ok": False})
                return
        yield _sse(
            "stage_done",
            {
                "stage": stage,
                "sizing_id": str(sizing["id"]),
                "converged": doc.converged,
                "markdown": render_markdown(doc),
            },
        )
        yield _sse("done", {"ok": doc.converged})
        return

    # Unreachable — VALID_STAGES guard above.


# ---------------------------------------------------------------------------
# Public — read
# ---------------------------------------------------------------------------

async def get_design_state(
    job_id: uuid.UUID,
    *,
    db: AsyncSession,
) -> DesignState:
    """Aggregate the pipeline state for a single design_circuit job.
    Joins jobs ⨝ specs ⨝ topology_selections ⨝ device_sizings; nullable
    fields reflect the furthest-completed stage."""
    job = await _fetch_design_job(db, job_id)
    spec = await _fetch_spec_for_job(db, job_id)
    sel = (
        await _fetch_latest_topology_selection(db, spec["id"])
        if spec else None
    )
    # §17.600 — union both sizing tables. _fetch_latest_device_sizing reads
    # only device_sizings (analog), so every digital_logic design reported
    # device_sizing_id=None even with a converged digital sizing (and a
    # formal_verdict=PASS). _fetch_latest_sizing_any_kind returns the same
    # {id, converged} shape across device_sizings + digital_sizings.
    sizing = (
        await _fetch_latest_sizing_any_kind(db, sel["id"])
        if sel else None
    )
    formal = (
        await _fetch_latest_formal_verification(db, sel["id"])
        if sel else None
    )
    return DesignState(
        job_id=job_id,
        job_type=str(job["job_type"]),
        status=str(job["status"]),
        brief=str(job["input_text"] or ""),
        created_at=job["created_at"],
        spec_id=(spec["id"] if spec else None),
        spec_confirmed_at=(spec["confirmed_at"] if spec else None),
        topology_selection_id=(sel["id"] if sel else None),
        device_sizing_id=(sizing["id"] if sizing else None),
        device_sizing_converged=(
            bool(sizing["converged"]) if sizing else None
        ),
        formal_verification_id=(formal["id"] if formal else None),
        formal_verdict=(formal["verdict"] if formal else None),
    )
