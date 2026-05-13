"""
Digital device-sizing stage — Verilator-in-the-loop counterpart to
the §17.147 analog ngspice loop.

Pipeline position: a topology_selection candidate with
``design.kind == 'digital_logic'`` is sized here. The loop is the
same shape as the analog one — LLM proposes (params + source) →
oracle runs → compare measurements to spec → feed gaps back to the
LLM next iter. The differences are entirely on the input/output
sides:

  * Source format: SystemVerilog, not SPICE.
  * Oracle: §17.141 Verilator sidecar, not §17.140 ngspice.
  * Measurement protocol: ``$display("KPI <name>=<value>")`` lines
    parsed by the sidecar, not ``.meas`` results.
  * Top-module name: Verilator requires it explicitly; v1 fixes
    it as ``tb`` by convention.

Persists ``digital_sizings`` rows (migration 044) — schema mirror of
``device_sizings`` with ``final_sv_source`` + ``top_module`` instead
of ``final_netlist``. The shared ``sim_runs`` table holds the
underlying attestations with ``tool='verilator'``.

Same audit-the-attempt invariant as §17.147: every call persists a
row (converged or not). API-layer ``ok`` mirrors ``converged``.
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
from app.sim.device_sizing import (
    CandidateIndexError,
    TopologySelectionNotFoundError,
    _candidate_to_dict,
    _check_constraints,
    _fetch_topology_selection,
)
from app.sim.spec_store import (
    SpecNotConfirmedError,
    SpecNotFoundError,
    require_confirmed_spec,
)
from app.sim.verilator import run_verilator
from app.utils.llm_parsing import parse_json_object

logger = logging.getLogger("scaffold")

DEFAULT_TOP_MODULE = "tb"
_FEEDBACK_TRUNCATE = 1500


_SYSTEM_PROMPT = (
    "You are a digital circuit sizing assistant. Given (a) a "
    "confirmed engineering spec with `design.kind = 'digital_logic'`, "
    "(b) a chosen topology candidate, and (c) optionally one or more "
    "prior simulation results, emit a JSON object with two fields:\n"
    "  - params: design parameter values (e.g. WIDTH=4, "
    "CLK_PERIOD_NS=10).\n"
    "  - sv_source: a SystemVerilog source string containing BOTH the "
    "DUT and a self-checking testbench module named `tb`.\n"
    "\n"
    "Output ONLY the JSON object — no prose, no markdown fences, no "
    "explanation outside the JSON.\n"
    "\n"
    "The `sv_source` field is a SINGLE JSON STRING. Newlines inside "
    "the string MUST be `\\n` JSON escapes — do NOT split across "
    "multiple JSON values, and do NOT use Python-style concatenated "
    "string literals.\n"
    "\n"
    "================================================================\n"
    "WORKED EXAMPLE — 4-bit counter, target wrap_count=16 at "
    "clk_period_ns=10:\n"
    "================================================================\n"
    "\n"
    "Conceptually, the .sv source for this example is the following "
    "(this is the literal text Verilator will read):\n"
    "\n"
    "    module counter #(parameter WIDTH = 4) (\n"
    "        input  logic clk,\n"
    "        input  logic rst_n,\n"
    "        output logic [WIDTH-1:0] count\n"
    "    );\n"
    "        always_ff @(posedge clk or negedge rst_n) begin\n"
    "            if (!rst_n) count <= '0;\n"
    "            else        count <= count + 1'b1;\n"
    "        end\n"
    "    endmodule\n"
    "\n"
    "    module tb;\n"
    "        localparam WIDTH = 4;\n"
    "        logic clk = 0, rst_n = 0;\n"
    "        logic [WIDTH-1:0] count;\n"
    "        int wrap_count = 0;\n"
    "\n"
    "        counter #(.WIDTH(WIDTH)) dut (.clk(clk), .rst_n(rst_n), "
    ".count(count));\n"
    "\n"
    "        always #5 clk = ~clk;  // 10 ns period\n"
    "\n"
    "        initial begin\n"
    "            rst_n = 0;\n"
    "            #20 rst_n = 1;\n"
    "            // §17.155 canonical wrap-detection pattern.\n"
    "            // SAMPLE at negedge (half a cycle after the posedge,\n"
    "            // by which time NBA updates to `count` have settled).\n"
    "            // Each iteration: one negedge fires, count reflects\n"
    "            // the increment from the prior posedge; increment\n"
    "            // wrap_count; break when count has wrapped to 0.\n"
    "            // For an N-bit counter starting at 0 this produces\n"
    "            // exactly 2^N negedges before the wrap is observed.\n"
    "            wrap_count = 0;\n"
    "            forever begin\n"
    "                @(negedge clk);\n"
    "                wrap_count = wrap_count + 1;\n"
    "                if (count == 0) break;\n"
    "            end\n"
    "            $display(\"KPI wrap_count=%0d\", wrap_count);\n"
    "            $display(\"KPI clk_period_ns=10\");\n"
    "            $finish;\n"
    "        end\n"
    "    endmodule\n"
    "\n"
    "Your JSON output for the example above (single-line `sv_source` "
    "with `\\n` escapes):\n"
    "\n"
    '{\"params\":{\"WIDTH\":\"4\",\"CLK_PERIOD_NS\":\"10\"},'
    '\"sv_source\":'
    '\"module counter #(parameter WIDTH = 4) (\\n  input  logic clk,\\n  '
    'input  logic rst_n,\\n  output logic [WIDTH-1:0] count\\n);\\n  '
    'always_ff @(posedge clk or negedge rst_n) begin\\n    if (!rst_n) '
    'count <= \'0;\\n    else        count <= count + 1\'b1;\\n  end\\n'
    'endmodule\\nmodule tb;\\n  ... (testbench body) ...\\nendmodule\\n\"}\n'
    "\n"
    "================================================================\n"
    "VERILATOR 5.x BATCH-MODE RULES — read carefully:\n"
    "================================================================\n"
    "\n"
    "1. The TOP module MUST be named `tb`. The orchestrator's "
    "Verilator wrapper passes `--top-module tb` and runs the resulting "
    "`obj_dir/Vtb` binary. Any other top-module name fails to build.\n"
    "\n"
    "2. The `tb` module MUST emit one `$display(\"KPI <name>=<value>\")` "
    "line per measurable constraint, where `<name>` matches the spec's "
    "constraint id verbatim. The orchestrator parses these into the "
    "measurements payload. Example: spec constraint id=`wrap_count` → "
    "`$display(\"KPI wrap_count=%0d\", wrap_count)`.\n"
    "\n"
    "3. The `tb` module MUST end its initial block with `$finish`. "
    "Without it Verilator runs forever and the sidecar's timeout "
    "kicks in.\n"
    "\n"
    "4. Use SystemVerilog (.sv) syntax — `logic` not `wire/reg`, "
    "`always_ff` not `always`, `localparam` for compile-time constants.\n"
    "\n"
    "5. Verilator 5.024 treats WIDTHEXPAND/WIDTHTRUNC warnings as "
    "errors. Be width-clean: cast loop counters to bit widths "
    "(`expected = 8'(8'hA0 + i[7:0])`), or declare loop variables "
    "with matching width.\n"
    "\n"
    "================================================================\n"
    "COMMON PITFALLS — these failure modes WILL be rejected:\n"
    "================================================================\n"
    "\n"
    "PITFALL 1: Driving stimulus on `@(posedge clk)`. In Verilator's "
    "event order the testbench resumes BEFORE the DUT's `always_ff` "
    "samples — the FIFO sees the NEXT iteration's value, not the "
    "current one (§17.141's smoke discovery).\n"
    "FIX: Drive stimulus at NEGEDGE — `@(negedge clk); wr_en = 1; "
    "din = value;` — and let the DUT sample at the following posedge.\n"
    "\n"
    "PITFALL 2: Width mismatch (e.g. `din = 8'hA0 + i;` where `i` is "
    "`int`). Verilator 5.024 fails with `%Error: Exiting due to N "
    "warning(s)` for WIDTHEXPAND. FIX: cast — "
    "`din = 8'(8'hA0 + i[7:0])`.\n"
    "\n"
    "PITFALL 3: Forgetting `$finish` in the `initial` block. Build "
    "succeeds; the run hangs until the sidecar timeout fires. FIX: "
    "Always end the initial block with `$finish`.\n"
    "\n"
    "PITFALL 4: Naming the top module anything other than `tb`. "
    "FIX: Whatever your DUT is called, wrap it in a `module tb; ... "
    "endmodule` and put the testbench logic there.\n"
    "\n"
    "PITFALL 5: Using `wire/reg` (Verilog-1995) instead of "
    "`logic` (SystemVerilog). The `--binary --timing` flow rejects "
    "Verilog-1995 outside compatibility mode. FIX: declare every signal "
    "as `logic`.\n"
    "\n"
    "PITFALL 6: Sampling DUT outputs immediately after `@(posedge clk)`. "
    "The testbench's `initial` resumes in the ACTIVE region — BEFORE "
    "the DUT's non-blocking assignments (NBA) for that posedge have "
    "fired. So `count` read at this point is the PRE-edge value, not "
    "the post-edge one. For a counter, this means the first iteration "
    "sees `count == 0` (the pre-first-increment value) and breaks "
    "immediately — measured wrap_count is 1, target is 2^N.\n"
    "FIX: SAMPLE at the next negedge — by then a half-cycle has passed "
    "and the NBA has settled. Canonical pattern:\n"
    "  `wrap_count = 0; forever begin @(negedge clk); wrap_count = "
    "wrap_count + 1; if (count == 0) break; end`\n"
    "For an N-bit counter starting at 0 after reset, this produces "
    "exactly 2^N negedges before count returns to 0 — wrap_count = "
    "2^N. The same negedge-sampling rule applies to ALL DUT-output "
    "observation, not just wrap detection: read combinational and "
    "registered DUT outputs at negedge, never on the bare posedge.\n"
    "\n"
    "PITFALL 7: Off-by-one in cycle accounting at the loop boundary. "
    "Two common ways to get this wrong even with negedge sampling:\n"
    "  (a) Sampling `count == 0` BEFORE incrementing wrap_count — you "
    "miss counting the wrap-cycle itself.\n"
    "  (b) Initialising wrap_count to 1 (instead of 0) AND incrementing "
    "in the loop body — you over-count by one.\n"
    "FIX: Initialise wrap_count = 0, then in each loop iteration: "
    "(1) await negedge, (2) increment wrap_count, (3) check `count == "
    "0` for break. The increment-then-check order makes the wrap-cycle "
    "itself the last one counted.\n"
    "\n"
    "================================================================\n"
    "MEASUREMENT SEMANTICS — match the constraint id verbatim:\n"
    "================================================================\n"
    "\n"
    "Each spec constraint has an `id`, a `kind`, a unit, and a target "
    "value. Read the description field carefully — the same `id` can "
    "have different operational meanings in different specs:\n"
    "\n"
    "  - `wrap_count` (timing.latency, cycles): canonical interpretation "
    "is 'number of clock edges from t=0 until the counter returns to "
    "its initial value' (= 2^WIDTH for an N-bit binary counter).\n"
    "  - `clk_period_ns` (timing.period, ns): nanoseconds between "
    "consecutive posedges of the system clock. Constant for a given "
    "testbench; emit it directly as `$display(\"KPI clk_period_ns=%0d\", "
    "<period_value>)`.\n"
    "  - `latency_cycles` (timing.latency, cycles): clock cycles from "
    "an input event to its corresponding output event. Measure with two "
    "timestamps and subtract.\n"
    "  - `errors` (when present): integer count of testbench assertion "
    "failures. Target is typically 0 with `max: 0`.\n"
    "\n"
    "When the constraint description names a specific operational "
    "semantics (e.g. 'cycles between resets to first wrap'), implement "
    "EXACTLY that — do not substitute a 'close enough' definition. The "
    "constraint-checker is strict: measured = target ± tolerance_pct, "
    "and a wrap_count of 15 vs 16 fails a ±5% tolerance.\n"
    "\n"
    "================================================================\n"
    "ITERATIVE REFINEMENT (when prior iterations are provided):\n"
    "================================================================\n"
    "\n"
    "- Measurement out of tolerance: review the constraint formula and "
    "adjust parameters. E.g. for a counter, wrap_count = 2^WIDTH.\n"
    "- `verilator exit=1` + WIDTHEXPAND warnings: fix the width "
    "mismatch (PITFALL 2). Add explicit casts.\n"
    "- `verilator exit=1` + 'top module not found': you named your top "
    "module something other than `tb` (PITFALL 4). Rename.\n"
    "- KPI line missing for a required constraint id: you forgot the "
    "`$display(\"KPI <id>=...\")` line. Add it.\n"
    "- Simulation timed out: missing `$finish` (PITFALL 3). Add it.\n"
)


@dataclass
class DigitalIterationRecord:
    iteration: int
    params: dict[str, str]
    sv_source: str
    sim_run_id: uuid.UUID | None
    measurements: dict[str, float]
    gaps: list[str]
    verilator_ok: bool
    verilator_stderr_tail: str


@dataclass
class DigitalSizingResult:
    ok: bool
    sizing_id: uuid.UUID | None = None
    spec_id: uuid.UUID | None = None
    topology_selection_id: uuid.UUID | None = None
    candidate_idx: int = 0
    converged: bool = False
    iterations: int = 0
    final_params: dict[str, str] = field(default_factory=dict)
    final_sv_source: str = ""
    top_module: str = DEFAULT_TOP_MODULE
    final_measurements: dict[str, float] = field(default_factory=dict)
    sim_run_ids: list[uuid.UUID] = field(default_factory=list)
    model_used: str = ""
    errors: list[str] = field(default_factory=list)
    iteration_history: list[DigitalIterationRecord] = field(default_factory=list)


async def _call_llm_propose_sv(
    spec_json: dict[str, Any],
    candidate: dict[str, Any],
    history: list[DigitalIterationRecord],
    *,
    role: str,
) -> tuple[dict[str, Any] | None, str, str, str | None]:
    """Return (parsed_body, raw_text, model_used, error)."""
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
                f"verilator_ok={h.verilator_ok}"
            )
            if h.gaps:
                user_lines.append("  gaps: " + "; ".join(h.gaps))
            if not h.verilator_ok and h.verilator_stderr_tail:
                user_lines.append(
                    "  verilator_stderr_tail: "
                    + h.verilator_stderr_tail[:_FEEDBACK_TRUNCATE]
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
    sv_source = parsed.get("sv_source")
    if not isinstance(params, dict) or not isinstance(sv_source, str) or not sv_source.strip():
        return None, resp.text, resp.model or role, (
            "missing or empty 'params'/'sv_source' field"
        )
    return parsed, resp.text, resp.model or role, None


async def _insert_digital_sizing(
    db: AsyncSession,
    *,
    spec_id: uuid.UUID,
    topology_selection_id: uuid.UUID,
    candidate_idx: int,
    final_params: dict[str, str],
    final_sv_source: str,
    top_module: str,
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
            INSERT INTO digital_sizings (
                spec_id, topology_selection_id, candidate_idx,
                final_params, final_sv_source, top_module,
                sim_run_ids, converged, iterations, model_used,
                measurements_final, errors
            )
            VALUES (
                :spec_id, :topology_selection_id, :candidate_idx,
                CAST(:final_params AS JSONB), :final_sv_source,
                :top_module,
                :sim_run_ids, :converged, :iterations, :model_used,
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
            "final_sv_source": final_sv_source,
            "top_module": top_module,
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


async def size_digital_device(
    topology_selection_id: uuid.UUID,
    *,
    db: AsyncSession,
    candidate_idx: int = 0,
    max_iterations: int | None = None,
    model_role: str | None = None,
    top_module: str = DEFAULT_TOP_MODULE,
) -> DigitalSizingResult:
    """Verilator-in-the-loop sizing for ``design.kind = 'digital_logic'``.

    Mirrors the §17.147 ``size_device`` shape — never raises on
    LLM/Verilator failure (TopologySelectionNotFoundError /
    CandidateIndexError do raise for HTTP-404/400 mapping). Persists
    a row even on non-convergence.
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

    try:
        spec_row = await require_confirmed_spec(db, spec_id)
    except SpecNotConfirmedError:
        return DigitalSizingResult(
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
    if design_kind != "digital_logic":
        return DigitalSizingResult(
            ok=False,
            spec_id=spec_id,
            topology_selection_id=topology_selection_id,
            candidate_idx=candidate_idx,
            errors=[
                f"size_digital_device only handles design.kind="
                f"'digital_logic'; got {design_kind!r}"
            ],
        )

    role = model_role or settings.spec_extractor_model_role
    history: list[DigitalIterationRecord] = []
    converged = False
    model_used = role
    last_params: dict[str, str] = {}
    last_sv: str = ""
    last_measurements: dict[str, float] = {}
    sim_run_ids: list[uuid.UUID] = []
    loop_errors: list[str] = []

    for i in range(1, max_iterations + 1):
        body, raw_text, model_used, llm_err = await _call_llm_propose_sv(
            spec_json, candidate, history, role=role,
        )
        if body is None:
            loop_errors.append(f"iter {i}: LLM proposal failed: {llm_err}")
            history.append(DigitalIterationRecord(
                iteration=i,
                params={},
                sv_source="",
                sim_run_id=None,
                measurements={},
                gaps=[f"llm_proposal_failed: {llm_err}"],
                verilator_ok=False,
                verilator_stderr_tail=raw_text[-_FEEDBACK_TRUNCATE:],
            ))
            continue

        params = dict(body.get("params") or {})
        sv_source = str(body.get("sv_source") or "")
        last_params = params
        last_sv = sv_source

        sim = await run_verilator(
            sv_source, top_module=top_module, db=db,
        )
        if sim.sim_run_id is not None:
            sim_run_ids.append(sim.sim_run_id)

        measurements = sim.measurements
        last_measurements = measurements
        verilator_ok = sim.ok
        stderr_tail = (sim.stderr or "")[-_FEEDBACK_TRUNCATE:]

        gaps: list[str] = []
        if not verilator_ok:
            gaps.append(
                f"verilator exit={sim.exit_code} timed_out={sim.timed_out} "
                f"build_failed={sim.build_failed}"
            )
        else:
            gaps = _check_constraints(spec_json, measurements)

        history.append(DigitalIterationRecord(
            iteration=i,
            params=params,
            sv_source=sv_source,
            sim_run_id=sim.sim_run_id,
            measurements=measurements,
            gaps=gaps,
            verilator_ok=verilator_ok,
            verilator_stderr_tail=stderr_tail,
        ))

        if verilator_ok and not gaps:
            converged = True
            break

    if not converged and not loop_errors:
        loop_errors.append(
            f"budget exhausted after {len(history)} iterations; "
            f"final gaps: {history[-1].gaps if history else 'no_history'}"
        )

    sizing_id = await _insert_digital_sizing(
        db,
        spec_id=spec_id,
        topology_selection_id=topology_selection_id,
        candidate_idx=candidate_idx,
        final_params=last_params,
        final_sv_source=last_sv,
        top_module=top_module,
        sim_run_ids=sim_run_ids,
        converged=converged,
        iterations=len(history),
        model_used=model_used,
        measurements_final=last_measurements,
        errors=loop_errors,
    )

    return DigitalSizingResult(
        ok=converged,
        sizing_id=sizing_id,
        spec_id=spec_id,
        topology_selection_id=topology_selection_id,
        candidate_idx=candidate_idx,
        converged=converged,
        iterations=len(history),
        final_params=last_params,
        final_sv_source=last_sv,
        top_module=top_module,
        final_measurements=last_measurements,
        sim_run_ids=sim_run_ids,
        model_used=model_used,
        errors=loop_errors,
        iteration_history=history,
    )
