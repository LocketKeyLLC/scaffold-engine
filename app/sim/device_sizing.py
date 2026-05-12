"""
Device-sizing stage — first CLOSED-LOOP stage of the engineering-
design pipeline (§17.147).

Takes a confirmed spec + a chosen topology candidate from a persisted
``topology_selections`` row, drives an LLM/SPICE iteration loop:

    1. LLM proposes parameter values + an ngspice .cir netlist
       (.control/.endc form, .meas names matching constraint ids).
    2. Run ngspice via the §17.140 sidecar wrapper. One sim_runs row
       written per iteration — that's the audit-per-attempt invariant
       upheld by ``app.sim.ngspice``.
    3. Compare measurements to the spec's constraints. If every
       constraint is within tolerance: ``converged = True``, done.
    4. Otherwise: feed (params, measurements, gaps, ngspice stderr)
       back to the LLM as prior-iteration context and loop.
    5. Loop is bounded by ``settings.device_sizing_max_iterations``
       (default 3). On budget exhaustion the row is still persisted
       with ``converged = False`` so the operator can audit *what was
       tried* rather than ask "did the pipeline silently give up?".

Differences from §17.146 (topology_select):

  * Persists a row even when the loop fails to converge. The
    ``device_sizings`` row records the *attempt*; ``converged`` and
    ``errors`` carry the outcome.
  * ``ok`` (API-layer) is True iff ``converged == True``. The
    distinction matters because a non-converged row is still an
    auditable artefact, but the wider pipeline can't accept it as
    "the design works."

Analog-only for v1. ``design.kind != "analog_circuit"`` is refused at
the stage entry with a clear error message; digital sizing (verilator-
in-the-loop) is a separate stage on the deferred list.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app import model_router
from app.config import settings
from app.sim.ngspice import run_ngspice
from app.sim.spec_store import (
    SpecNotConfirmedError,
    SpecNotFoundError,
    require_confirmed_spec,
)
from app.utils.llm_parsing import parse_json_object

logger = logging.getLogger("scaffold")

# Default tolerance applied to constraints that have a ``target`` but
# no ``tolerance_pct``. 1% is tight enough to catch real drift while
# not failing on numeric-precision noise from ngspice.
DEFAULT_TOLERANCE_PCT = 1.0

# Cap on how much of an ngspice stderr/stdout we pipe back to the LLM
# on the next iteration. ngspice tail can be ~1 KB; we want enough for
# the LLM to see the error type without paging the whole prompt.
_FEEDBACK_TRUNCATE = 1500


_SYSTEM_PROMPT = (
    "You are an analog circuit sizing assistant. Given (a) a confirmed "
    "engineering spec, (b) a chosen topology candidate, and (c) "
    "optionally one or more prior simulation results, emit a JSON "
    "object with two fields:\n"
    "  - params: component values (e.g. R1=1k, C1=159.155n).\n"
    "  - netlist: an ngspice 44.x batch-mode .cir file string.\n"
    "\n"
    "Output ONLY the JSON object — no prose, no markdown fences, no "
    "explanation outside the JSON.\n"
    "\n"
    "Output shape:\n"
    "The `netlist` field is a SINGLE JSON STRING containing the entire "
    ".cir text. Newlines inside the string MUST be `\\n` JSON escapes — "
    "do NOT split the netlist across multiple JSON values, and do NOT "
    "use Python-style concatenated string literals like `\"line1\\n\" "
    "\"line2\\n\"` (that is not valid JSON and the orchestrator will "
    "fail to parse it).\n"
    "\n"
    "================================================================\n"
    "WORKED EXAMPLE — RC low-pass at fc_3db target 1000 Hz (±5%):\n"
    "================================================================\n"
    "\n"
    "Conceptually, the .cir file content for this example is the "
    "following 9 lines (this is the literal text ngspice will read):\n"
    "\n"
    "    * RC low-pass fc=1000Hz\n"
    "    V1 in 0 AC 1\n"
    "    R1 in out 1.59155k\n"
    "    C1 out 0 100n\n"
    "    .control\n"
    "    ac dec 100 10 100k\n"
    "    meas ac fc_3db when vdb(out)=-3 fall=1\n"
    "    .endc\n"
    "    .end\n"
    "\n"
    "Your JSON output is one object with `params` and `netlist`. The "
    "netlist is a single string with `\\n` between lines:\n"
    "\n"
    '{\"params\":{\"R1\":\"1.59155k\",\"C1\":\"100n\"},'
    '\"netlist\":\"* RC low-pass fc=1000Hz\\nV1 in 0 AC 1\\n'
    'R1 in out 1.59155k\\nC1 out 0 100n\\n.control\\n'
    'ac dec 100 10 100k\\nmeas ac fc_3db when vdb(out)=-3 fall=1\\n'
    '.endc\\n.end\\n\"}\n'
    "\n"
    "Analytical sizing for this example: fc = 1/(2π R C). For target "
    "fc=1000 Hz pick C=100nF (a standard value), then R = 1/(2π · 1000 · "
    "100e-9) = 1591.55 Ω → 1.59155 kΩ.\n"
    "\n"
    "================================================================\n"
    "NGSPICE 44.x BATCH-MODE RULES — read carefully:\n"
    "================================================================\n"
    "\n"
    "1. Netlist structure (in this order, no exceptions):\n"
    "   a. Title line — first line, starts with `*`. Comment only.\n"
    "   b. Component cards — V/I sources, R, L, C, etc. One per line.\n"
    "   c. `.control` block containing the analysis and measurements.\n"
    "   d. `.end` — final line.\n"
    "\n"
    "2. The `.control` block is REQUIRED. It must contain:\n"
    "   - An analysis line (`ac dec <ppd> <fmin> <fmax>`, "
    "`tran <step> <stop>`, `dc <src> <vstart> <vstop> <vstep>`).\n"
    "   - One `meas` line per measurable constraint.\n"
    "   - Close with `.endc` before `.end`.\n"
    "\n"
    "3. `meas` syntax — every `meas` line MUST start with the analysis "
    "type token (`ac` / `dc` / `tran` / `sp`). Examples:\n"
    "   - AC -3 dB corner: `meas ac fc_3db when vdb(out)=-3 fall=1`\n"
    "   - AC mid-band gain: `meas ac gain_dc find vdb(out) at=1`\n"
    "   - Transient settling: "
    "`meas tran t_settle when v(out)=0.99 cross=1`\n"
    "\n"
    "4. AC analysis frequency range MUST span the constraint target. "
    "Rule of thumb: `fmin = target / 100`, `fmax = target * 100`. For "
    "1 kHz target use `ac dec 100 10 100k`.\n"
    "\n"
    "5. The `meas` result name MUST match the spec's constraint id "
    "EXACTLY. If the spec has `\"id\": \"fc_3db\"`, the meas line is "
    "`meas ac fc_3db when ...`. The orchestrator's measurement-vs-"
    "constraint check is name-based, so any drift is a failure.\n"
    "\n"
    "================================================================\n"
    "COMMON PITFALLS — these failure modes WILL be rejected:\n"
    "================================================================\n"
    "\n"
    "PITFALL 1: `meas` outside `.control`. ngspice 44.x rejects with\n"
    "  `Error: measure limited to tran, dc, sp, or ac analysis`.\n"
    "FIX: Put every `meas` line INSIDE the `.control` / `.endc` block, "
    "and start the `meas` line with the analysis-type token (`ac` etc.).\n"
    "\n"
    "PITFALL 2: Using `mag(v(node))=0.7071` to find -3 dB corner. The "
    "ngspice measure-expression parser fails this form with:\n"
    "  `meas fc_3db find freq when mag(v(out))=0.7071 failed!`\n"
    "FIX: Use `vdb(node)=-3` instead. `vdb` returns 20·log10 of the AC "
    "magnitude relative to the input source's AC magnitude, so the -3 "
    "dB point is exactly `vdb(out)=-3`.\n"
    "\n"
    "PITFALL 3: Omitting `fall=1` (or `rise=1`) in a `when` clause. "
    "Without a direction selector ngspice may not converge on the "
    "intended crossing. ALWAYS include `fall=N` or `rise=N`.\n"
    "\n"
    "PITFALL 4: Forgetting `V1 ... AC 1` for AC analyses. Without an "
    "AC stimulus magnitude on the source, the AC analysis runs but all "
    "node voltages stay zero. Spec a magnitude of `1` so `vdb(out)` is "
    "directly the gain in dB.\n"
    "\n"
    "PITFALL 5: Using `.meas` as a top-level card (with the leading "
    "dot). In ngspice 44.x batch mode this is the same failure as "
    "PITFALL 1. The form inside `.control` is `meas` (no leading dot).\n"
    "\n"
    "================================================================\n"
    "ITERATIVE REFINEMENT (when prior iterations are provided):\n"
    "================================================================\n"
    "\n"
    "- Measurement out of tolerance: adjust analytically. For RC LPF, "
    "fc ∝ 1/(R·C); if measured fc is K× too high, scale C (or R) by K.\n"
    "- `ngspice exit=1` + `Error: measure limited to ...` in stderr: "
    "you put `meas` outside `.control` (PITFALL 1/5). Fix the placement, "
    "keep the params the same.\n"
    "- `meas ... failed!` in stdout: the analysis range didn't bracket "
    "the crossing, OR you used a `mag()` expression (PITFALL 2). Widen "
    "the AC range to `target/100 … target*100` and switch to `vdb`.\n"
    "- Required constraint reported as `not_measured`: you omitted the "
    "`meas` line for that constraint id. Add it.\n"
    "- DO NOT randomly perturb parameters. Analytical scaling first; "
    "syntax fixes second.\n"
)


@dataclass
class IterationRecord:
    iteration: int
    params: dict[str, str]
    netlist: str
    sim_run_id: uuid.UUID | None
    measurements: dict[str, float]
    gaps: list[str]
    ngspice_ok: bool
    ngspice_stderr_tail: str


@dataclass
class DeviceSizingResult:
    ok: bool
    sizing_id: uuid.UUID | None = None
    spec_id: uuid.UUID | None = None
    topology_selection_id: uuid.UUID | None = None
    candidate_idx: int = 0
    converged: bool = False
    iterations: int = 0
    final_params: dict[str, str] = field(default_factory=dict)
    final_netlist: str = ""
    final_measurements: dict[str, float] = field(default_factory=dict)
    sim_run_ids: list[uuid.UUID] = field(default_factory=list)
    model_used: str = ""
    errors: list[str] = field(default_factory=list)
    iteration_history: list[IterationRecord] = field(default_factory=list)


class TopologySelectionNotFoundError(LookupError):
    """Raised when the topology_selection_id is unknown."""


class CandidateIndexError(LookupError):
    """Raised when candidate_idx is out of bounds."""


async def _fetch_topology_selection(
    db: AsyncSession, selection_id: uuid.UUID
) -> dict[str, Any]:
    result = await db.execute(
        text(
            """
            SELECT id, spec_id, candidates, rag_chunk_ids
            FROM topology_selections
            WHERE id = :id
            """
        ),
        {"id": str(selection_id)},
    )
    row = result.mappings().first()
    if row is None:
        raise TopologySelectionNotFoundError(
            f"topology_selection {selection_id} not found"
        )
    return dict(row)


_MEASURABLE_KIND_PREFIXES = ("electrical.", "timing.", "thermal.", "signal.")


def _is_measurable_kind(kind: str | None) -> bool:
    """True if this constraint kind names a quantity the ngspice
    oracle is expected to be able to measure. Non-measurable kinds
    (cost.*, physical.*) are skipped — they're verified outside the
    sizing loop."""
    return bool(kind) and any(
        kind.startswith(p) for p in _MEASURABLE_KIND_PREFIXES
    )


def _check_constraints(
    spec_json: dict[str, Any],
    measurements: dict[str, float],
) -> list[str]:
    """Walk the spec's constraints, return a list of gap descriptions
    for the ones the measurements miss. Empty list ⇒ every required +
    measurable constraint is in-tolerance.

    A required measurable constraint with NO corresponding measurement
    is a gap — the LLM forgot to emit the .meas line, and we MUST NOT
    silently report convergence on an unmeasured spec. The next
    iteration's prompt feedback will tell the LLM to add the .meas.

    Non-measurable kinds (cost.*, physical.*) are skipped: ngspice
    can't observe them and the sizing loop doesn't try to.
    Constraints with ``criticality != "required"`` are skipped when
    unmeasured (preferred / best_effort don't block convergence).
    """
    gaps: list[str] = []
    for c in spec_json.get("constraints", []):
        cid = c.get("id")
        kind = c.get("kind", "")
        if cid is None:
            continue

        if cid not in measurements:
            if _is_measurable_kind(kind) and c.get("criticality", "required") == "required":
                gaps.append(
                    f"{cid}: required {kind} constraint not measured — "
                    f"LLM must emit `.meas` with name '{cid}'"
                )
            continue

        measured = measurements[cid]
        target = c.get("target")
        cmin = c.get("min")
        cmax = c.get("max")
        unit = c.get("unit", "")

        # target + tolerance check
        if target is not None:
            tol_pct = c.get("tolerance_pct", DEFAULT_TOLERANCE_PCT)
            band = abs(target) * (tol_pct / 100.0)
            if abs(measured - target) > band:
                gaps.append(
                    f"{cid}: measured {measured:g} {unit}, target "
                    f"{target:g} {unit} ±{tol_pct:g}% — out of tolerance"
                )
                continue

        # min check
        if cmin is not None and measured < cmin:
            gaps.append(
                f"{cid}: measured {measured:g} {unit} < min {cmin:g} {unit}"
            )
            continue

        # max check
        if cmax is not None and measured > cmax:
            gaps.append(
                f"{cid}: measured {measured:g} {unit} > max {cmax:g} {unit}"
            )
            continue

    return gaps


def _candidate_to_dict(c: Any) -> dict[str, Any]:
    if isinstance(c, dict):
        return c
    return {}


async def _call_llm_propose(
    spec_json: dict[str, Any],
    candidate: dict[str, Any],
    history: list[IterationRecord],
    *,
    role: str,
) -> tuple[dict[str, Any] | None, str, str, str | None]:
    """Return (parsed_body, raw_text, model_used, error). parsed_body
    is None when the LLM call failed or the output didn't parse."""
    user_lines: list[str] = []
    user_lines.append("Spec (validated JSON):")
    user_lines.append(json.dumps(spec_json, separators=(",", ":")))
    user_lines.append("")
    user_lines.append("Topology candidate:")
    user_lines.append(
        f"name: {candidate.get('name', '')}\n"
        f"description: {candidate.get('description', '')}\n"
        f"rationale: {candidate.get('rationale', '')}"
    )
    if history:
        user_lines.append("")
        user_lines.append("Prior iterations (most recent last):")
        for h in history:
            user_lines.append(
                f"iter {h.iteration} params={json.dumps(h.params)} "
                f"measurements={json.dumps(h.measurements)} "
                f"ngspice_ok={h.ngspice_ok}"
            )
            if h.gaps:
                user_lines.append(
                    "  gaps: " + "; ".join(h.gaps)
                )
            if not h.ngspice_ok and h.ngspice_stderr_tail:
                user_lines.append(
                    "  ngspice_stderr_tail: "
                    + h.ngspice_stderr_tail[:_FEEDBACK_TRUNCATE]
                )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_lines)},
    ]
    resp = await model_router.chat(
        messages=messages,
        role=role,
        temperature=0.0,
        max_tokens=4096,
    )
    if not resp.success or not (resp.text or "").strip():
        return None, resp.text or "", resp.model or role, (
            resp.error or "empty response"
        )

    parsed = parse_json_object(resp.text)
    if not isinstance(parsed, dict):
        return None, resp.text, resp.model or role, "did not parse as JSON object"

    params = parsed.get("params")
    netlist = parsed.get("netlist")
    if not isinstance(params, dict) or not isinstance(netlist, str) or not netlist.strip():
        return None, resp.text, resp.model or role, (
            "missing or empty 'params'/'netlist' field"
        )
    return parsed, resp.text, resp.model or role, None


async def _insert_sizing(
    db: AsyncSession,
    *,
    spec_id: uuid.UUID,
    topology_selection_id: uuid.UUID,
    candidate_idx: int,
    final_params: dict[str, str],
    final_netlist: str,
    sim_run_ids: list[uuid.UUID],
    converged: bool,
    iterations: int,
    model_used: str,
    measurements_final: dict[str, float],
    errors: list[str],
) -> uuid.UUID:
    row = await db.execute(
        text(
            """
            INSERT INTO device_sizings (
                spec_id, topology_selection_id, candidate_idx,
                final_params, final_netlist, sim_run_ids,
                converged, iterations, model_used,
                measurements_final, errors
            )
            VALUES (
                :spec_id, :topology_selection_id, :candidate_idx,
                CAST(:final_params AS JSONB), :final_netlist, :sim_run_ids,
                :converged, :iterations, :model_used,
                CAST(:measurements_final AS JSONB), :errors
            )
            RETURNING id
            """
        ),
        {
            "spec_id": str(spec_id),
            "topology_selection_id": str(topology_selection_id),
            "candidate_idx": candidate_idx,
            "final_params": json.dumps(final_params),
            "final_netlist": final_netlist,
            "sim_run_ids": [str(sid) for sid in sim_run_ids],
            "converged": converged,
            "iterations": iterations,
            "model_used": model_used,
            "measurements_final": json.dumps(measurements_final),
            "errors": errors,
        },
    )
    sid = row.scalar_one()
    await db.commit()
    return sid


async def size_device(
    topology_selection_id: uuid.UUID,
    *,
    db: AsyncSession,
    candidate_idx: int = 0,
    max_iterations: int | None = None,
    model_role: str | None = None,
) -> DeviceSizingResult:
    """Run the closed-loop sizing stage against a topology candidate.

    Always returns a ``DeviceSizingResult``; never raises on LLM /
    ngspice failure. The two lookup errors (topology_selection_id
    missing, candidate_idx out of bounds) raise
    ``TopologySelectionNotFoundError`` / ``CandidateIndexError`` so
    the HTTP layer maps them to 404 / 400 respectively — those are
    programmer errors, not runtime data conditions.
    """
    if max_iterations is None:
        max_iterations = settings.device_sizing_max_iterations

    sel_row = await _fetch_topology_selection(db, topology_selection_id)
    candidates = sel_row["candidates"] or []
    if not (0 <= candidate_idx < len(candidates)):
        raise CandidateIndexError(
            f"candidate_idx={candidate_idx} out of range "
            f"(0..{len(candidates) - 1})"
        )
    candidate = _candidate_to_dict(candidates[candidate_idx])
    spec_id = sel_row["spec_id"]

    # Gate: confirmed spec required.
    try:
        spec_row = await require_confirmed_spec(db, spec_id)
    except SpecNotConfirmedError:
        return DeviceSizingResult(
            ok=False,
            spec_id=spec_id,
            topology_selection_id=topology_selection_id,
            candidate_idx=candidate_idx,
            errors=[
                f"spec {spec_id} is not confirmed; POST "
                f"/specs/{spec_id}/confirm first"
            ],
        )

    spec_json = spec_row.spec_json
    design_kind = (spec_json.get("design") or {}).get("kind")
    if design_kind != "analog_circuit":
        return DeviceSizingResult(
            ok=False,
            spec_id=spec_id,
            topology_selection_id=topology_selection_id,
            candidate_idx=candidate_idx,
            errors=[
                f"v1 device-sizing only handles design.kind="
                f"'analog_circuit'; got {design_kind!r}"
            ],
        )

    role = model_role or settings.spec_extractor_model_role
    history: list[IterationRecord] = []
    converged = False
    model_used = role
    last_params: dict[str, str] = {}
    last_netlist = ""
    last_measurements: dict[str, float] = {}
    sim_run_ids: list[uuid.UUID] = []
    loop_errors: list[str] = []

    for i in range(1, max_iterations + 1):
        body, raw_text, model_used, llm_err = await _call_llm_propose(
            spec_json, candidate, history, role=role,
        )
        if body is None:
            loop_errors.append(f"iter {i}: LLM proposal failed: {llm_err}")
            # Record as a degenerate iteration so the audit row shows it.
            history.append(IterationRecord(
                iteration=i,
                params={},
                netlist="",
                sim_run_id=None,
                measurements={},
                gaps=[f"llm_proposal_failed: {llm_err}"],
                ngspice_ok=False,
                ngspice_stderr_tail=raw_text[-_FEEDBACK_TRUNCATE:],
            ))
            continue

        params = dict(body.get("params") or {})
        netlist = str(body.get("netlist") or "")
        last_params = params
        last_netlist = netlist

        sim = await run_ngspice(netlist, db=db)
        if sim.sim_run_id is not None:
            sim_run_ids.append(sim.sim_run_id)

        measurements = sim.measurements
        last_measurements = measurements
        ngspice_ok = sim.ok
        stderr_tail = (sim.stderr or "")[-_FEEDBACK_TRUNCATE:]

        gaps = []
        if not ngspice_ok:
            gaps.append(
                f"ngspice exit={sim.exit_code} timed_out={sim.timed_out}"
            )
        else:
            gaps = _check_constraints(spec_json, measurements)

        history.append(IterationRecord(
            iteration=i,
            params=params,
            netlist=netlist,
            sim_run_id=sim.sim_run_id,
            measurements=measurements,
            gaps=gaps,
            ngspice_ok=ngspice_ok,
            ngspice_stderr_tail=stderr_tail,
        ))

        if ngspice_ok and not gaps:
            converged = True
            break

    if not converged and not loop_errors:
        loop_errors.append(
            f"budget exhausted after {len(history)} iterations; "
            f"final gaps: {history[-1].gaps if history else 'no_history'}"
        )

    sizing_id = await _insert_sizing(
        db,
        spec_id=spec_id,
        topology_selection_id=topology_selection_id,
        candidate_idx=candidate_idx,
        final_params=last_params,
        final_netlist=last_netlist,
        sim_run_ids=sim_run_ids,
        converged=converged,
        iterations=len(history),
        model_used=model_used,
        measurements_final=last_measurements,
        errors=loop_errors,
    )

    return DeviceSizingResult(
        ok=converged,
        sizing_id=sizing_id,
        spec_id=spec_id,
        topology_selection_id=topology_selection_id,
        candidate_idx=candidate_idx,
        converged=converged,
        iterations=len(history),
        final_params=last_params,
        final_netlist=last_netlist,
        final_measurements=last_measurements,
        sim_run_ids=sim_run_ids,
        model_used=model_used,
        errors=loop_errors,
        iteration_history=history,
    )
