"""
Report stage — the engineering-design pipeline's terminal renderer.

**Defining invariant:** the report is regenerable from the audit
tables alone. No LLM, no new data, no judgement calls beyond
"classify this measurement against this constraint." Same input rows
through the same code produce byte-identical output. That's what
makes the report an *attestation* rather than another LLM artefact.

What gets joined:

  specs ──┐
          ├─ design_name / kind / description / constraints / interfaces / environment
          │
  topology_selections ──┐
                        ├─ selected candidate at sizing.candidate_idx
                        │   (name / description / rationale / citations[])
                        │
  device_sizings ──┐
                   ├─ converged / iterations / final_params / final_netlist
                   │   final_measurements / errors / model_used
                   │
  sim_runs ──┐
             ├─ joined by sim_run_ids[] from the sizing row, one row
             │   per iteration of the §17.147 loop
             │
  Milvus chunks ──┐
                  ├─ best-effort fetch of cited entry_ids for content
                  │   snippets in the citations section. Missing chunks
                  │   render as "[content unavailable]" rather than
                  │   failing the report.

Per-constraint status classification mirrors §17.147's
``_check_constraints`` logic exactly — ``ok`` / ``out_of_tolerance``
/ ``violated_min`` / ``violated_max`` / ``not_measured`` / ``skipped``.
The same rule that gated the sizing loop's convergence is the rule
that labels each row in the measurement table.

Non-converged sizings ARE renderable (per the §17.148 design choice):
the Markdown carries a prominent banner and the audit section
surfaces the errors. The wider pipeline still treats only
``converged = TRUE`` as ready-to-ship, but a non-converged report is
the right post-mortem artefact for "why did this attempt fail?"
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.sim.device_sizing import DEFAULT_TOLERANCE_PCT, _is_measurable_kind
from app.utils.milvus_utils import escape_milvus_literal, get_collection

logger = logging.getLogger("scaffold")

REPORT_SCHEMA_VERSION = "1.0.0"

# Cap on chunk content embedded inline. The full chunk lives in
# Milvus; the report carries a head-of-chunk snippet so an operator
# reading the Markdown sees enough to remember what the citation is
# without paging the corpus.
_SNIPPET_CHARS = 800


class ReportNotAvailableError(LookupError):
    """Raised when the requested sizing_id has no row."""


@dataclass
class ReportConstraint:
    id: str
    kind: str
    description: str
    target: float | None
    min: float | None
    max: float | None
    tolerance_pct: float | None
    unit: str
    criticality: str
    measured: float | None
    # Enum: "ok" | "out_of_tolerance" | "violated_min" | "violated_max"
    #       | "not_measured" | "skipped"
    status: str


@dataclass
class ReportCitation:
    entry_id: str
    title: str = ""
    snippet: str = ""
    source_url: str = ""
    available: bool = False


@dataclass
class ReportSimRun:
    sim_run_id: uuid.UUID
    iteration: int
    tool: str
    tool_version: str
    exit_code: int
    timed_out: bool
    duration_ms: int
    measurements: dict[str, float]
    verdict: str | None


@dataclass
class ReportDocument:
    report_schema_version: str
    generated_at: datetime
    sizing_id: uuid.UUID
    spec_id: uuid.UUID
    topology_selection_id: uuid.UUID
    candidate_idx: int
    converged: bool
    iterations: int
    design_name: str
    design_kind: str
    design_description: str
    spec_schema_version: str
    constraints: list[ReportConstraint] = field(default_factory=list)
    interfaces: list[dict[str, Any]] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict)
    selected_topology: dict[str, str] = field(default_factory=dict)
    citations: list[ReportCitation] = field(default_factory=list)
    final_params: dict[str, str] = field(default_factory=dict)
    # §17.153 — kind discriminator + per-kind source fields. Analog
    # designs populate ``final_netlist`` (SPICE); digital designs
    # populate ``final_sv_source`` + ``top_module`` (SystemVerilog).
    # The unused field for each kind stays as the empty default.
    kind: str = "analog"
    final_netlist: str = ""
    final_sv_source: str = ""
    top_module: str = ""
    final_measurements: dict[str, float] = field(default_factory=dict)
    sim_runs: list[ReportSimRun] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    model_used: str = ""


# ---------------------------------------------------------------------------
# DB joins
# ---------------------------------------------------------------------------

async def _fetch_sizing(db: AsyncSession, sizing_id: uuid.UUID) -> dict[str, Any]:
    """Fetch sizing row from device_sizings, falling back to
    digital_sizings (§17.153). Returns a dict with a synthesized
    ``kind`` field — ``'analog'`` or ``'digital'`` — so the caller can
    branch rendering without a second DB round-trip.

    Both tables carry the same column set except for the source-text
    column (``final_netlist`` for analog, ``final_sv_source`` + ``top_module``
    for digital). The returned dict normalises into a superset shape
    with both source columns populated for the relevant kind."""
    # Try analog first — the table that existed since §17.147.
    row = await db.execute(
        text(
            """
            SELECT id, spec_id, topology_selection_id, candidate_idx,
                   final_params, final_netlist, sim_run_ids, converged,
                   iterations, model_used, measurements_final, errors,
                   created_at
            FROM device_sizings
            WHERE id = :id
            """
        ),
        {"id": str(sizing_id)},
    )
    r = row.mappings().first()
    if r is not None:
        d = dict(r)
        d["kind"] = "analog"
        d["final_sv_source"] = ""
        d["top_module"] = ""
        return d

    # §17.152 — fall back to digital_sizings.
    row = await db.execute(
        text(
            """
            SELECT id, spec_id, topology_selection_id, candidate_idx,
                   final_params, final_sv_source, top_module,
                   sim_run_ids, converged, iterations, model_used,
                   measurements_final, errors, created_at
            FROM digital_sizings
            WHERE id = :id
            """
        ),
        {"id": str(sizing_id)},
    )
    r = row.mappings().first()
    if r is not None:
        d = dict(r)
        d["kind"] = "digital"
        d["final_netlist"] = ""  # not applicable
        return d

    raise ReportNotAvailableError(
        f"sizing {sizing_id} not found in device_sizings or digital_sizings"
    )


async def _fetch_spec(db: AsyncSession, spec_id: uuid.UUID) -> dict[str, Any]:
    row = await db.execute(
        text(
            """
            SELECT id, schema_version, spec_json
            FROM specs
            WHERE id = :id
            """
        ),
        {"id": str(spec_id)},
    )
    r = row.mappings().first()
    if r is None:
        raise ReportNotAvailableError(
            f"specs {spec_id} not found (referenced by sizing)"
        )
    return dict(r)


async def _fetch_topology_selection(
    db: AsyncSession, sel_id: uuid.UUID
) -> dict[str, Any]:
    row = await db.execute(
        text(
            """
            SELECT id, spec_id, candidates, rag_chunk_ids, model_used
            FROM topology_selections
            WHERE id = :id
            """
        ),
        {"id": str(sel_id)},
    )
    r = row.mappings().first()
    if r is None:
        raise ReportNotAvailableError(
            f"topology_selections {sel_id} not found (referenced by sizing)"
        )
    return dict(r)


async def _fetch_sim_runs(
    db: AsyncSession, sim_run_ids: list[Any]
) -> dict[uuid.UUID, dict[str, Any]]:
    """Fetch sim_runs rows by id. Returns a map keyed by UUID so the
    caller can look up in the order of ``sim_run_ids[]`` (which
    encodes iteration order — §17.147 appends per iter)."""
    if not sim_run_ids:
        return {}
    ids = [str(s) for s in sim_run_ids]
    row = await db.execute(
        text(
            """
            SELECT id, tool, tool_version, exit_code, timed_out,
                   duration_ms, measurements, verdict
            FROM sim_runs
            WHERE id = ANY(CAST(:ids AS uuid[]))
            """
        ),
        {"ids": ids},
    )
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for r in row.mappings().all():
        rid = r["id"]
        if isinstance(rid, str):
            rid = uuid.UUID(rid)
        out[rid] = dict(r)
    return out


# ---------------------------------------------------------------------------
# Milvus best-effort chunk fetch
# ---------------------------------------------------------------------------

async def _fetch_chunk_content(
    entry_ids: list[str],
) -> dict[str, dict[str, str]]:
    """Best-effort fetch of chunk content by entry_id from Milvus.

    Returns a map keyed by entry_id. Missing entries are silently
    omitted — a citation that can't be resolved renders as
    ``[content unavailable]`` rather than failing the whole report.
    Reading the corpus is a query-time side-effect; the report is
    still regenerable from the DB alone if Milvus is unreachable.
    """
    if not entry_ids:
        return {}
    try:
        loop = asyncio.get_running_loop()
        collection = await loop.run_in_executor(None, get_collection)
        if collection is None:
            return {}
        quoted = [f'"{escape_milvus_literal(e)}"' for e in entry_ids]
        expr = f"entry_id in [{', '.join(quoted)}]"

        def _sync():
            return collection.query(
                expr=expr,
                output_fields=["entry_id", "title", "canonical_text", "source_url"],
                limit=max(1, len(entry_ids) * 2),
            )

        rows = await loop.run_in_executor(None, _sync)
        return {
            r["entry_id"]: {
                "title": r.get("title", "") or "",
                "content": r.get("canonical_text", "") or "",
                "source_url": r.get("source_url", "") or "",
            }
            for r in rows
            if r.get("entry_id")
        }
    except Exception as exc:
        logger.warning("report.chunk_content_fetch_failed: %s", exc)
        return {}


# ---------------------------------------------------------------------------
# Constraint status classification
# ---------------------------------------------------------------------------

def _classify_constraint(
    c: dict[str, Any],
    measurements: dict[str, float],
) -> tuple[str, float | None]:
    """Same labelling rule §17.147's ``_check_constraints`` uses, but
    returns the *status enum* rather than a gap-description list.
    Status values match the strings in ``ReportConstraint.status``."""
    cid = c.get("id", "")
    kind = c.get("kind", "")
    measured = measurements.get(cid) if cid else None

    if measured is None:
        if _is_measurable_kind(kind):
            return "not_measured", None
        return "skipped", None

    target = c.get("target")
    cmin = c.get("min")
    cmax = c.get("max")

    if target is not None:
        tol_pct = c.get("tolerance_pct", DEFAULT_TOLERANCE_PCT)
        if abs(measured - target) > abs(target) * (tol_pct / 100.0):
            return "out_of_tolerance", measured

    if cmin is not None and measured < cmin:
        return "violated_min", measured

    if cmax is not None and measured > cmax:
        return "violated_max", measured

    return "ok", measured


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def build_report(
    sizing_id: uuid.UUID,
    *,
    db: AsyncSession,
    generated_at: datetime | None = None,
) -> ReportDocument:
    """Assemble a ``ReportDocument`` from the audit tables.

    Raises ``ReportNotAvailableError`` if any of the referenced rows
    are missing — these are programmer / data-integrity conditions
    (you can't generate a report for a sizing that doesn't exist),
    distinct from "the sizing is non-converged" (which is a normal
    state the report handles).

    ``generated_at`` is the only externally-injected non-deterministic
    field; callers can pass a fixed value for testing.
    """
    sizing = await _fetch_sizing(db, sizing_id)
    spec = await _fetch_spec(db, sizing["spec_id"])
    selection = await _fetch_topology_selection(
        db, sizing["topology_selection_id"]
    )
    sim_run_map = await _fetch_sim_runs(db, sizing["sim_run_ids"] or [])

    candidates: list[Any] = selection["candidates"] or []
    candidate_idx = sizing["candidate_idx"]
    if not (0 <= candidate_idx < len(candidates)):
        raise ReportNotAvailableError(
            f"candidate_idx={candidate_idx} out of range for "
            f"selection {selection['id']}"
        )
    candidate: dict[str, Any] = candidates[candidate_idx] or {}
    citation_ids = [
        c for c in (candidate.get("citations") or []) if isinstance(c, str)
    ]
    chunk_map = await _fetch_chunk_content(citation_ids)

    spec_json: dict[str, Any] = spec["spec_json"] or {}
    design: dict[str, Any] = spec_json.get("design") or {}
    measurements_final: dict[str, float] = sizing["measurements_final"] or {}

    constraints: list[ReportConstraint] = []
    for c in spec_json.get("constraints") or []:
        status, measured = _classify_constraint(c, measurements_final)
        constraints.append(
            ReportConstraint(
                id=str(c.get("id", "")),
                kind=str(c.get("kind", "")),
                description=str(c.get("description", "")),
                target=c.get("target"),
                min=c.get("min"),
                max=c.get("max"),
                tolerance_pct=c.get("tolerance_pct"),
                unit=str(c.get("unit", "")),
                criticality=str(c.get("criticality", "required")),
                measured=measured,
                status=status,
            )
        )

    citations: list[ReportCitation] = []
    for cid in citation_ids:
        chunk = chunk_map.get(cid)
        if chunk:
            citations.append(
                ReportCitation(
                    entry_id=cid,
                    title=chunk["title"],
                    snippet=chunk["content"][:_SNIPPET_CHARS],
                    source_url=chunk["source_url"],
                    available=True,
                )
            )
        else:
            citations.append(ReportCitation(entry_id=cid, available=False))

    sim_runs_list: list[ReportSimRun] = []
    for iter_idx, raw in enumerate(sizing["sim_run_ids"] or [], start=1):
        key = uuid.UUID(str(raw)) if not isinstance(raw, uuid.UUID) else raw
        r = sim_run_map.get(key)
        if r is None:
            continue
        sim_runs_list.append(
            ReportSimRun(
                sim_run_id=key,
                iteration=iter_idx,
                tool=str(r["tool"]),
                tool_version=str(r["tool_version"]),
                exit_code=int(r["exit_code"]),
                timed_out=bool(r["timed_out"]),
                duration_ms=int(r["duration_ms"]),
                measurements=dict(r["measurements"] or {}),
                verdict=r.get("verdict"),
            )
        )

    return ReportDocument(
        report_schema_version=REPORT_SCHEMA_VERSION,
        generated_at=generated_at or datetime.now(timezone.utc),
        sizing_id=sizing["id"] if isinstance(sizing["id"], uuid.UUID)
                  else uuid.UUID(str(sizing["id"])),
        spec_id=sizing["spec_id"] if isinstance(sizing["spec_id"], uuid.UUID)
                else uuid.UUID(str(sizing["spec_id"])),
        topology_selection_id=(
            sizing["topology_selection_id"]
            if isinstance(sizing["topology_selection_id"], uuid.UUID)
            else uuid.UUID(str(sizing["topology_selection_id"]))
        ),
        candidate_idx=candidate_idx,
        converged=bool(sizing["converged"]),
        iterations=int(sizing["iterations"]),
        design_name=str(design.get("name", "")),
        design_kind=str(design.get("kind", "")),
        design_description=str(design.get("description", "")),
        spec_schema_version=str(spec["schema_version"]),
        constraints=constraints,
        interfaces=list(spec_json.get("interfaces") or []),
        environment=dict(spec_json.get("environment") or {}),
        selected_topology={
            "name": str(candidate.get("name", "")),
            "description": str(candidate.get("description", "")),
            "rationale": str(candidate.get("rationale", "")),
        },
        citations=citations,
        final_params=dict(sizing["final_params"] or {}),
        kind=str(sizing.get("kind", "analog")),
        final_netlist=str(sizing.get("final_netlist") or ""),
        final_sv_source=str(sizing.get("final_sv_source") or ""),
        top_module=str(sizing.get("top_module") or ""),
        final_measurements=measurements_final,
        sim_runs=sim_runs_list,
        errors=list(sizing["errors"] or []),
        model_used=str(sizing["model_used"] or ""),
    )


# ---------------------------------------------------------------------------
# Markdown rendering — pure, deterministic.
# ---------------------------------------------------------------------------

def _fmt_num(v: Any) -> str:
    """Stable scalar formatter — fixed precision so byte-identical
    output on repeated renders of the same doc."""
    if v is None:
        return "—"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def render_markdown(doc: ReportDocument) -> str:
    """Render the report as Markdown. Pure function — same doc in,
    byte-identical string out. No LLM, no network, no clock reads."""
    lines: list[str] = []
    lines.append(f"# {doc.design_name or '<unnamed design>'}")
    lines.append("")

    if not doc.converged:
        lines.append(
            "> ⚠ **NOT CONVERGED** — this sizing attempt did not "
            "meet all required constraints. See the **Audit — "
            "Diagnostics** section below for the failure trail."
        )
        lines.append("")

    lines.append(f"- **Report schema:** {doc.report_schema_version}")
    lines.append(f"- **Kind:** {doc.kind}")
    lines.append(f"- **Sizing ID:** `{doc.sizing_id}`")
    lines.append(f"- **Spec ID:** `{doc.spec_id}`")
    lines.append(f"- **Topology selection ID:** `{doc.topology_selection_id}`")
    lines.append(f"- **Candidate index:** {doc.candidate_idx}")
    lines.append(f"- **Generated at:** {doc.generated_at.isoformat()}")
    lines.append(f"- **Converged:** {'yes' if doc.converged else 'no'}")
    lines.append(f"- **Iterations:** {doc.iterations}")
    lines.append(f"- **Model used:** `{doc.model_used or '<unknown>'}`")
    lines.append("")

    lines.append("## Spec")
    lines.append(f"- **Kind:** `{doc.design_kind}`")
    if doc.design_description:
        lines.append(f"- **Description:** {doc.design_description}")
    lines.append("")
    lines.append("### Constraints")
    lines.append(
        "| ID | Kind | Target | Min | Max | Tol % | Unit | Crit | "
        "Measured | Status |"
    )
    lines.append("|---|---|---|---|---|---|---|---|---|---|")
    for c in doc.constraints:
        lines.append(
            f"| `{c.id}` | {c.kind} | {_fmt_num(c.target)} | "
            f"{_fmt_num(c.min)} | {_fmt_num(c.max)} | "
            f"{_fmt_num(c.tolerance_pct)} | {c.unit or '—'} | "
            f"{c.criticality} | {_fmt_num(c.measured)} | "
            f"**{c.status}** |"
        )
    lines.append("")

    if doc.interfaces:
        lines.append("### Interfaces")
        lines.append("| ID | Direction | Kind |")
        lines.append("|---|---|---|")
        for iface in doc.interfaces:
            lines.append(
                f"| `{iface.get('id', '')}` | "
                f"{iface.get('direction', '')} | "
                f"{iface.get('kind', '')} |"
            )
        lines.append("")

    if doc.environment:
        lines.append("### Environment")
        for k in sorted(doc.environment.keys()):
            v = doc.environment[k]
            lines.append(f"- **{k}:** {v}")
        lines.append("")

    lines.append("## Topology")
    name = doc.selected_topology.get("name") or "<unnamed>"
    desc = doc.selected_topology.get("description") or ""
    rationale = doc.selected_topology.get("rationale") or ""
    lines.append(f"**{name}**")
    lines.append("")
    if desc:
        lines.append(desc)
        lines.append("")
    if rationale:
        lines.append("### Rationale")
        lines.append(rationale)
        lines.append("")

    if doc.citations:
        lines.append("### Citations")
        for cite in doc.citations:
            if cite.available:
                title = cite.title or "<untitled>"
                lines.append(f"- `{cite.entry_id}` — **{title}**")
                if cite.source_url:
                    lines.append(f"  - Source: {cite.source_url}")
                if cite.snippet:
                    snippet = cite.snippet.replace("\n", " ")
                    lines.append(f"  - > {snippet}")
            else:
                lines.append(
                    f"- `{cite.entry_id}` — *[content unavailable]*"
                )
        lines.append("")

    lines.append("## Sized Parameters")
    if doc.final_params:
        lines.append("| Component | Value |")
        lines.append("|---|---|")
        for k in sorted(doc.final_params.keys()):
            lines.append(f"| `{k}` | `{doc.final_params[k]}` |")
    else:
        lines.append("_(none recorded)_")
    lines.append("")

    measurable = [
        c for c in doc.constraints if c.status != "skipped"
    ]
    lines.append("## Measurements vs Targets")
    if measurable:
        lines.append("| Constraint | Target | Measured | Status |")
        lines.append("|---|---|---|---|")
        for c in measurable:
            if c.target is not None:
                target = (
                    f"{_fmt_num(c.target)} {c.unit}".strip()
                    if c.tolerance_pct is None
                    else f"{_fmt_num(c.target)} ±{_fmt_num(c.tolerance_pct)}% {c.unit}".strip()
                )
            else:
                lo = _fmt_num(c.min) if c.min is not None else "—"
                hi = _fmt_num(c.max) if c.max is not None else "—"
                target = f"[{lo}, {hi}] {c.unit}".strip()
            measured = (
                f"{_fmt_num(c.measured)} {c.unit}".strip()
                if c.measured is not None
                else "_(not measured)_"
            )
            lines.append(
                f"| `{c.id}` | {target} | {measured} | **{c.status}** |"
            )
    else:
        lines.append("_(no measurable constraints in spec)_")
    lines.append("")

    lines.append("## Sim Run Manifest")
    if doc.sim_runs:
        lines.append(
            "| Iter | Sim Run ID | Tool | Version | Exit | Timed Out "
            "| Duration ms | Measurements | Verdict |"
        )
        lines.append(
            "|---|---|---|---|---|---|---|---|---|"
        )
        for r in doc.sim_runs:
            meas = ", ".join(
                f"{k}={_fmt_num(v)}" for k, v in sorted(r.measurements.items())
            ) or "_(none)_"
            lines.append(
                f"| {r.iteration} | `{r.sim_run_id}` | {r.tool} | "
                f"{r.tool_version} | {r.exit_code} | "
                f"{'true' if r.timed_out else 'false'} | {r.duration_ms} | "
                f"{meas} | {r.verdict or '—'} |"
            )
    else:
        lines.append("_(no simulation runs recorded)_")
    lines.append("")

    if doc.errors:
        lines.append("## Audit — Diagnostics")
        for e in doc.errors:
            lines.append(f"- {e}")
        lines.append("")

    # §17.153 — kind-aware source section. Analog renders the SPICE
    # netlist; digital renders the SystemVerilog source with the
    # testbench module name in the section title.
    if doc.kind == "digital" and doc.final_sv_source:
        top = doc.top_module or "tb"
        lines.append(f"## Final SystemVerilog Source (top: `{top}`)")
        lines.append("```systemverilog")
        lines.append(doc.final_sv_source.rstrip())
        lines.append("```")
        lines.append("")
    elif doc.final_netlist:
        lines.append("## Final Netlist")
        lines.append("```spice")
        lines.append(doc.final_netlist.rstrip())
        lines.append("```")
        lines.append("")

    return "\n".join(lines)
