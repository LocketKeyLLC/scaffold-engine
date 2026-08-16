"""
Formal-verification stage — symbiyosys-in-the-loop closed-loop repair
(§17.414). Sits between the §17.152 digital-sizing stage and the §17.148
report stage; applies ONLY to ``design.kind == 'digital_logic'``.

Pipeline position: a *converged* ``digital_sizings`` row (Verilator-proven
DUT) is the starting point. This stage drives a formal-verification loop:

    1. LLM emits {dut, properties}:
       * ``dut``        — a formal-clean, synthesizable version of the sized
                          DUT (Yosys ``-formal`` subset — no ``$display`` /
                          ``#delay`` / testbench).
       * ``properties`` — a SystemVerilog harness module named ``formal_top``
                          that instantiates the DUT and carries the SVA
                          ``assert``/``assume``/``cover`` properties derived
                          from the spec's constraints.
    2. Run symbiyosys (§17.142 sidecar wrapper) on ``dut + properties`` with
       ``top_module='formal_top'``. One ``sim_runs`` row per attempt
       (tool='symbiyosys', verdict column populated) — the audit-per-attempt
       invariant from §17.140/§17.147.
    3. verdict == PASS → converged, done. verdict == FAIL → counterexample fed
       back; the LLM revises the DUT and re-verifies. Loop bounded by
       ``settings.formal_verify_max_iterations`` (default 3).

PROPERTY-LOCKING (anti-gaming): an LLM in a repair loop could weaken an
assertion to escape a real FAIL. Once any attempt returns a *real verdict*
(PASS or FAIL — meaning the SVA compiled and BMC ran), the ``properties``
harness is FROZEN: every later iteration reuses it verbatim and only the DUT
may change. Before that (ERROR / UNKNOWN / TIMEOUT from non-compiling SVA or
an inconclusive bound) the properties may still be revised. The freeze is
enforced in code, not just the prompt — the LLM's ``properties`` field is
ignored once locked.

Same contracts as the sizing stages:
  * Never raises on LLM / sby failure — surfaces as
    ``FormalVerifyResult(ok=False, errors=[...])``.
  * The two lookup errors (digital_sizing_id missing, candidate_idx out of
    bounds) raise so the HTTP layer maps them to 404 / 400.
  * Persists a ``formal_verifications`` row even on non-convergence — the row
    IS the attempt; ``converged`` (= verdict PASS) is the outcome.
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
from app.utils.llm_retry import chat_until_nonempty
from app.sim.device_sizing import (
    CandidateIndexError,
    _candidate_to_dict,
    _fetch_topology_selection,
)
from app.sim.spec_store import (
    SpecNotConfirmedError,
    require_confirmed_spec,
)
from app.sim.symbiyosys import (
    VERDICT_FAIL,
    VERDICT_PASS,
    run_symbiyosys,
)
from app.utils.llm_parsing import parse_json_object

logger = logging.getLogger("scaffold")

# Formal harness top-module name. The LLM's ``properties`` field MUST define
# ``module formal_top ... endmodule`` instantiating the DUT (the formal
# counterpart of the digital sizer's fixed ``tb`` convention, §17.155).
DEFAULT_FORMAL_TOP = "formal_top"

_FEEDBACK_TRUNCATE = 1500

# Verdicts that mean "the SVA compiled and BMC actually ran" — the trigger to
# freeze the property set.
_REAL_VERDICTS = frozenset({VERDICT_PASS, VERDICT_FAIL})


_SYSTEM_PROMPT = (
    "You are a digital formal-verification assistant. You are given (a) a "
    "confirmed engineering spec with `design.kind = 'digital_logic'`, (b) a "
    "chosen topology candidate, (c) a DUT that already passed dynamic "
    "(Verilator) simulation, and optionally (d) prior formal-verification "
    "attempts. Emit a JSON object with exactly two fields:\n"
    "  - dut: a formal-clean, SYNTHESIZABLE SystemVerilog source for the "
    "design under test.\n"
    "  - properties: a SystemVerilog source defining `module formal_top "
    "(input logic clk);` that instantiates the DUT and contains the formal "
    "assertions.\n"
    "\n"
    "Output ONLY the JSON object — no prose, no markdown fences. Both fields "
    "are SINGLE JSON STRINGS; newlines are `\\n` escapes, no Python-style "
    "concatenation.\n"
    "\n"
    "================================================================\n"
    "THE TOOLCHAIN — Yosys `read -formal` + smtbmc (NO Verific). Read this:\n"
    "================================================================\n"
    "The harness is verified by `read -formal design.sv; prep -top "
    "formal_top` then bounded model checking with smtbmc. This frontend has "
    "a RESTRICTED SVA subset. The rules below are NOT style preferences — "
    "violating any of them produces verdict=ERROR (the SV fails to compile).\n"
    "\n"
    "1. TOP MODULE: `module formal_top (input logic clk);` — `clk` is its "
    "ONLY port. The model checker advances `clk` itself, one BMC step per "
    "cycle. Do NOT generate a clock. NEVER write `always #5 clk = ~clk;`, "
    "any `#<delay>`, `$display`, `$finish`, an `initial` block, or a "
    "testbench — those are simulation constructs and are NOT synthesizable.\n"
    "\n"
    "2. ASSERTIONS: use IMMEDIATE assertions inside a clocked always block "
    "ONLY:\n"
    "      always @(posedge clk) begin\n"
    "        assert (<boolean expression>);\n"
    "      end\n"
    "   This frontend does NOT support concurrent SVA property syntax: "
    "`assert property (@(posedge clk) ...)`, `assume property (...)`, and "
    "`cover property (...)` all fail with `syntax error, unexpected '@'`. "
    "Use only the immediate forms `assert(...)`, `assume(...)`, `cover(...)`. "
    "Do NOT use `$past`, `$rose`, `$fell`, `$stable`, `|->`, `|=>`, or "
    "`disable iff` — none are reliable in this frontend.\n"
    "\n"
    "3. RESET: declare `rst_n` as a local `logic` in `formal_top` and model "
    "one reset pulse with a helper register + an `assume`:\n"
    "      logic rst_n;\n"
    "      reg init_done = 0;\n"
    "      always @(posedge clk) init_done <= 1'b1;\n"
    "      always @(*) if (!init_done) assume (!rst_n); else assume (rst_n);\n"
    "   This holds reset on the first step and releases it forever after, so "
    "the proof starts from a known reset state.\n"
    "\n"
    "4. PRIOR-VALUE properties: `$past` is unavailable — capture a previous "
    "value in an explicit shadow register (`always @(posedge clk) prev <= "
    "sig;`). BEWARE the reset-release boundary: with an async reset the DUT "
    "output can still hold its reset value for one cycle AFTER `init_done` "
    "flips, so guard such assertions to start a cycle later.\n"
    "\n"
    "5. Width-clean: Yosys is strict about widths. Match bit widths; cast "
    "where needed.\n"
    "\n"
    "================================================================\n"
    "WORKED EXAMPLE — 4-bit counter, property: count never overflows past "
    "15. This EXACT harness compiles and returns verdict=PASS.\n"
    "================================================================\n"
    "`dut` (synthesizable, no testbench):\n"
    "    module counter #(parameter WIDTH = 4) (\n"
    "      input  logic clk,\n"
    "      input  logic rst_n,\n"
    "      output logic [WIDTH-1:0] count\n"
    "    );\n"
    "      always_ff @(posedge clk or negedge rst_n) begin\n"
    "        if (!rst_n) count <= '0;\n"
    "        else        count <= count + 1'b1;\n"
    "      end\n"
    "    endmodule\n"
    "\n"
    "`properties` (module formal_top — instantiates DUT, models reset, "
    "asserts the spec constraint):\n"
    "    module formal_top (input logic clk);\n"
    "      logic rst_n;\n"
    "      logic [3:0] count;\n"
    "      counter #(.WIDTH(4)) dut (.clk(clk), .rst_n(rst_n), .count(count));\n"
    "\n"
    "      reg init_done = 0;\n"
    "      always @(posedge clk) init_done <= 1'b1;\n"
    "      always @(*) if (!init_done) assume (!rst_n); else assume (rst_n);\n"
    "\n"
    "      // spec constraint no_overflow (max 15)\n"
    "      always @(posedge clk) begin\n"
    "        if (init_done) assert (count <= 4'd15);\n"
    "      end\n"
    "    endmodule\n"
    "\n"
    "JSON output for the example (single-line strings, `\\n` escapes):\n"
    "{\"dut\":\"module counter #(parameter WIDTH = 4) (\\n  input  logic "
    "clk,\\n  input  logic rst_n,\\n  output logic [WIDTH-1:0] count\\n);\\n"
    "  always_ff @(posedge clk or negedge rst_n) begin\\n    if (!rst_n) "
    "count <= '0;\\n    else        count <= count + 1'b1;\\n  end\\n"
    "endmodule\\n\",\"properties\":\"module formal_top (input logic clk);\\n"
    "  logic rst_n;\\n  logic [3:0] count;\\n  counter #(.WIDTH(4)) dut "
    "(.clk(clk), .rst_n(rst_n), .count(count));\\n\\n  reg init_done = 0;\\n"
    "  always @(posedge clk) init_done <= 1'b1;\\n  always @(*) if "
    "(!init_done) assume (!rst_n); else assume (rst_n);\\n\\n  always "
    "@(posedge clk) begin\\n    if (init_done) assert (count <= 4'd15);\\n"
    "  end\\nendmodule\\n\"}\n"
    "\n"
    "================================================================\n"
    "DERIVING PROPERTIES FROM THE SPEC:\n"
    "================================================================\n"
    "- Each `constraints[]` entry with a numeric bound becomes an immediate "
    "assertion: `max: N` -> `assert (signal <= N);`; `min: N` -> "
    "`assert (signal >= N);`; a `target` for a derived signal -> equality.\n"
    "- Constrain the DUT's inputs from `formal_top` (tie them or bound them "
    "with `assume`) and read its outputs in the assertions.\n"
    "- Use `cover (<expr>);` (immediate) for a reachability witness if "
    "useful.\n"
    "\n"
    "================================================================\n"
    "ITERATIVE REPAIR (when prior attempts are provided):\n"
    "================================================================\n"
    "- verdict=ERROR means the SV did not compile/synthesize. The stdout "
    "tail names the file+line; the usual cause is a FORBIDDEN construct from "
    "the rules above (concurrent `assert property`, `$past`, `#delay`, "
    "`$display`, an `initial`/testbench). Remove it. You MAY revise the "
    "properties here — they are not yet frozen.\n"
    "- verdict=FAIL means BMC found a counterexample: the DUT VIOLATES a "
    "property. Fix the `dut` so the assertion holds. The `properties` are "
    "FROZEN — reuse them VERBATIM; change ONLY the `dut`. Do NOT weaken, "
    "delete, or relax any assertion to make it pass; that is a verification "
    "failure, not a fix.\n"
    "- verdict=UNKNOWN/TIMEOUT means BMC was inconclusive within the depth "
    "bound. Simplify the DUT; do not weaken the asserts.\n"
)


class DigitalSizingNotFoundError(LookupError):
    """Raised when the digital_sizing_id is unknown."""


@dataclass
class FormalIterationRecord:
    iteration: int
    dut: str
    properties: str
    sim_run_id: uuid.UUID | None
    verdict: str | None
    depth_reached: int | None
    gaps: list[str]
    properties_locked: bool
    sby_stderr_tail: str


@dataclass
class FormalVerifyResult:
    ok: bool
    formal_verification_id: uuid.UUID | None = None
    spec_id: uuid.UUID | None = None
    topology_selection_id: uuid.UUID | None = None
    digital_sizing_id: uuid.UUID | None = None
    candidate_idx: int = 0
    converged: bool = False
    verdict: str | None = None
    depth_reached: int | None = None
    iterations: int = 0
    final_dut: str = ""
    final_properties: str = ""
    top_module: str = DEFAULT_FORMAL_TOP
    mode: str = ""
    depth: int = 0
    engine: str = ""
    sim_run_ids: list[uuid.UUID] = field(default_factory=list)
    model_used: str = ""
    errors: list[str] = field(default_factory=list)
    iteration_history: list[FormalIterationRecord] = field(default_factory=list)


async def _fetch_digital_sizing(
    db: AsyncSession, digital_sizing_id: uuid.UUID
) -> dict[str, Any]:
    result = await db.execute(
        text(
            """
            SELECT id, spec_id, topology_selection_id, candidate_idx,
                   final_sv_source, top_module, converged
            FROM digital_sizings
            WHERE id = :id
            """
        ),
        {"id": str(digital_sizing_id)},
    )
    row = result.mappings().first()
    if row is None:
        raise DigitalSizingNotFoundError(
            f"digital_sizing {digital_sizing_id} not found"
        )
    return dict(row)


def _assemble_sv(dut: str, properties: str) -> str:
    """Concatenate the DUT and the formal harness into a single source the
    sidecar compiles together. The harness (``module formal_top``) references
    the DUT module by name."""
    return f"{dut.rstrip()}\n\n{properties.rstrip()}\n"


async def _call_llm_propose(
    spec_json: dict[str, Any],
    candidate: dict[str, Any],
    dut_seed: str,
    history: list[FormalIterationRecord],
    locked_properties: str | None,
    *,
    role: str,
) -> tuple[dict[str, Any] | None, str, str, str | None]:
    """Return (parsed_body, raw_text, model_used, error). parsed_body is None
    when the LLM call failed or the output didn't carry dut + properties."""
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
    user_lines.append("")
    user_lines.append(
        "Sized DUT (passed Verilator simulation; produce a formal-clean, "
        "synthesizable `dut` that preserves this behaviour — strip any "
        "testbench):"
    )
    user_lines.append(dut_seed[: _FEEDBACK_TRUNCATE * 4])
    if locked_properties is not None:
        user_lines.append("")
        user_lines.append(
            "FROZEN properties harness — reuse this VERBATIM as the "
            "`properties` field; revise ONLY the `dut`:"
        )
        user_lines.append(locked_properties)
    if history:
        user_lines.append("")
        user_lines.append("Prior formal attempts (most recent last):")
        for h in history:
            user_lines.append(
                f"iter {h.iteration} verdict={h.verdict} "
                f"depth_reached={h.depth_reached}"
            )
            if h.gaps:
                user_lines.append("  gaps: " + "; ".join(h.gaps))
            if h.sby_stderr_tail:
                user_lines.append(
                    "  sby_stderr_tail: "
                    + h.sby_stderr_tail[:_FEEDBACK_TRUNCATE]
                )

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(user_lines)},
    ]
    # §17.494 — retry-on-empty: role is the cloud thinking model whose
    # num_predict is a shared reasoning+content budget; a tight cap or unlucky
    # draw returns success=True + empty content (the §17.465 failure mode).
    resp = await chat_until_nonempty(
        model_router.chat,
        messages,
        {"role": role},
        temperature=0.0,
        max_tokens=settings.sim_stage_max_tokens,
        draws=settings.sim_stage_max_draws,
        label="formal_verify",
    )
    if not resp.success or not (resp.text or "").strip():
        return None, resp.text or "", resp.model or role, (
            resp.error or "empty response"
        )

    parsed = parse_json_object(resp.text)
    if not isinstance(parsed, dict):
        return None, resp.text, resp.model or role, "did not parse as JSON object"

    dut = parsed.get("dut")
    props = parsed.get("properties")
    if not isinstance(dut, str) or not dut.strip():
        return None, resp.text, resp.model or role, "missing or empty 'dut' field"
    # When properties are locked the LLM's properties field is ignored, so it
    # need not re-supply it; otherwise it is required.
    if locked_properties is None and (not isinstance(props, str) or not props.strip()):
        return None, resp.text, resp.model or role, (
            "missing or empty 'properties' field"
        )
    return parsed, resp.text, resp.model or role, None


async def _insert_formal_verification(
    db: AsyncSession,
    *,
    spec_id: uuid.UUID,
    topology_selection_id: uuid.UUID,
    digital_sizing_id: uuid.UUID,
    candidate_idx: int,
    dut_source: str,
    properties_source: str,
    top_module: str,
    mode: str,
    depth: int,
    engine: str,
    verdict: str | None,
    depth_reached: int | None,
    converged: bool,
    iterations: int,
    model_used: str,
    sim_run_ids: list[uuid.UUID],
    errors: list[str],
) -> uuid.UUID:
    row = await db.execute(
        text(
            """
            INSERT INTO formal_verifications (
                spec_id, topology_selection_id, digital_sizing_id,
                candidate_idx, dut_source, properties_source, top_module,
                mode, depth, engine, verdict, depth_reached,
                converged, iterations, model_used, sim_run_ids, errors
            )
            VALUES (
                :spec_id, :topology_selection_id, :digital_sizing_id,
                :candidate_idx, :dut_source, :properties_source, :top_module,
                :mode, :depth, :engine, :verdict, :depth_reached,
                :converged, :iterations, :model_used, :sim_run_ids, :errors
            )
            RETURNING id
            """
        ),
        {
            "spec_id": str(spec_id),
            "topology_selection_id": str(topology_selection_id),
            "digital_sizing_id": str(digital_sizing_id),
            "candidate_idx": candidate_idx,
            "dut_source": dut_source,
            "properties_source": properties_source,
            "top_module": top_module,
            "mode": mode,
            "depth": depth,
            "engine": engine,
            "verdict": verdict,
            "depth_reached": depth_reached,
            "converged": converged,
            "iterations": iterations,
            "model_used": model_used,
            "sim_run_ids": [str(sid) for sid in sim_run_ids],
            "errors": errors,
        },
    )
    fid = row.scalar_one()
    await db.commit()
    return fid


async def verify_design(
    digital_sizing_id: uuid.UUID,
    *,
    db: AsyncSession,
    mode: str | None = None,
    depth: int | None = None,
    engine: str = "smtbmc z3",
    max_iterations: int | None = None,
    model_role: str | None = None,
) -> FormalVerifyResult:
    """Run the closed-loop formal-verification stage against a digital sizing.

    Always returns a ``FormalVerifyResult``; never raises on LLM / sby
    failure. ``DigitalSizingNotFoundError`` / ``TopologySelectionNotFoundError``
    / ``CandidateIndexError`` raise so the HTTP layer maps them to 404 / 400.
    ``ok`` is True iff the final verdict is PASS.
    """
    if mode is None:
        mode = settings.formal_verify_mode
    if depth is None:
        depth = settings.formal_verify_depth
    if max_iterations is None:
        max_iterations = settings.formal_verify_max_iterations

    sizing = await _fetch_digital_sizing(db, digital_sizing_id)
    spec_id = sizing["spec_id"]
    topology_selection_id = sizing["topology_selection_id"]
    candidate_idx = sizing["candidate_idx"]
    dut_seed = sizing["final_sv_source"] or ""

    # Resolve the candidate for prompt context (and validate the index).
    sel_row = await _fetch_topology_selection(db, topology_selection_id)
    candidates = sel_row["candidates"] or []
    if not (0 <= candidate_idx < len(candidates)):
        raise CandidateIndexError(
            f"candidate_idx={candidate_idx} out of range "
            f"(0..{len(candidates) - 1})"
        )
    candidate = _candidate_to_dict(candidates[candidate_idx])

    # Gate: confirmed spec required.
    try:
        spec_row = await require_confirmed_spec(db, spec_id)
    except SpecNotConfirmedError:
        return FormalVerifyResult(
            ok=False,
            spec_id=spec_id,
            topology_selection_id=topology_selection_id,
            digital_sizing_id=digital_sizing_id,
            candidate_idx=candidate_idx,
            mode=mode,
            depth=depth,
            engine=engine,
            errors=[
                f"spec {spec_id} is not confirmed; POST "
                f"/specs/{spec_id}/confirm first"
            ],
        )

    spec_json = spec_row.spec_json
    design_kind = (spec_json.get("design") or {}).get("kind")
    if design_kind != "digital_logic":
        return FormalVerifyResult(
            ok=False,
            spec_id=spec_id,
            topology_selection_id=topology_selection_id,
            digital_sizing_id=digital_sizing_id,
            candidate_idx=candidate_idx,
            mode=mode,
            depth=depth,
            engine=engine,
            errors=[
                f"formal verification only handles design.kind="
                f"'digital_logic'; got {design_kind!r}"
            ],
        )

    role = model_role or settings.spec_extractor_model_role
    history: list[FormalIterationRecord] = []
    converged = False
    model_used = role
    last_dut = ""
    last_properties = ""
    last_verdict: str | None = None
    last_depth_reached: int | None = None
    sim_run_ids: list[uuid.UUID] = []
    loop_errors: list[str] = []
    locked_properties: str | None = None

    for i in range(1, max_iterations + 1):
        body, raw_text, model_used, llm_err = await _call_llm_propose(
            spec_json, candidate, dut_seed, history, locked_properties,
            role=role,
        )
        if body is None:
            loop_errors.append(f"iter {i}: LLM proposal failed: {llm_err}")
            history.append(FormalIterationRecord(
                iteration=i,
                dut="",
                properties=locked_properties or "",
                sim_run_id=None,
                verdict=None,
                depth_reached=None,
                gaps=[f"llm_proposal_failed: {llm_err}"],
                properties_locked=locked_properties is not None,
                sby_stderr_tail=raw_text[-_FEEDBACK_TRUNCATE:],
            ))
            continue

        dut = str(body.get("dut") or "")
        # Property-lock: once frozen, ignore the LLM's properties field.
        if locked_properties is not None:
            props = locked_properties
        else:
            props = str(body.get("properties") or "")
        last_dut = dut
        last_properties = props

        sv_source = _assemble_sv(dut, props)
        sim = await run_symbiyosys(
            sv_source,
            top_module=DEFAULT_FORMAL_TOP,
            db=db,
            mode=mode,
            depth=depth,
            engine=engine,
        )
        if sim.sim_run_id is not None:
            sim_run_ids.append(sim.sim_run_id)

        last_verdict = sim.verdict
        last_depth_reached = sim.depth_reached
        stderr_tail = (sim.stderr or "")[-_FEEDBACK_TRUNCATE:]

        # Freeze the property set the first time a real verdict is reached.
        if locked_properties is None and sim.verdict in _REAL_VERDICTS:
            locked_properties = props

        gaps: list[str] = []
        if sim.verdict == VERDICT_FAIL:
            gaps.append(
                f"verdict=FAIL: DUT violates a property "
                f"(counterexample at depth={sim.depth_reached}). Revise the "
                f"DUT; properties are frozen."
            )
        elif sim.verdict != VERDICT_PASS:
            gaps.append(
                f"verdict={sim.verdict}: not yet proven "
                f"(exit={sim.exit_code} timed_out={sim.timed_out})"
            )

        history.append(FormalIterationRecord(
            iteration=i,
            dut=dut,
            properties=props,
            sim_run_id=sim.sim_run_id,
            verdict=sim.verdict,
            depth_reached=sim.depth_reached,
            gaps=gaps,
            properties_locked=locked_properties is not None,
            sby_stderr_tail=stderr_tail,
        ))

        if sim.verdict == VERDICT_PASS:
            converged = True
            break

    if not converged and not loop_errors:
        loop_errors.append(
            f"not proven after {len(history)} iterations; "
            f"final verdict={last_verdict}"
        )

    fid = await _insert_formal_verification(
        db,
        spec_id=spec_id,
        topology_selection_id=topology_selection_id,
        digital_sizing_id=digital_sizing_id,
        candidate_idx=candidate_idx,
        dut_source=last_dut,
        properties_source=last_properties,
        top_module=DEFAULT_FORMAL_TOP,
        mode=mode,
        depth=depth,
        engine=engine,
        verdict=last_verdict,
        depth_reached=last_depth_reached,
        converged=converged,
        iterations=len(history),
        model_used=model_used,
        sim_run_ids=sim_run_ids,
        errors=loop_errors,
    )

    return FormalVerifyResult(
        ok=converged,
        formal_verification_id=fid,
        spec_id=spec_id,
        topology_selection_id=topology_selection_id,
        digital_sizing_id=digital_sizing_id,
        candidate_idx=candidate_idx,
        converged=converged,
        verdict=last_verdict,
        depth_reached=last_depth_reached,
        iterations=len(history),
        final_dut=last_dut,
        final_properties=last_properties,
        top_module=DEFAULT_FORMAL_TOP,
        mode=mode,
        depth=depth,
        engine=engine,
        sim_run_ids=sim_run_ids,
        model_used=model_used,
        errors=loop_errors,
        iteration_history=history,
    )
