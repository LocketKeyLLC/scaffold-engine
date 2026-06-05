"""
Integration test for the §17.414 formal-verify stage. Exercises the full
chain: real Postgres + real symbiyosys sidecar + real cloud LLM.

Drives ``verify_design`` directly (the verify stage has no standalone HTTP
surface — it's reachable via ``POST /design/{job_id}/advance?stage=verify``),
seeding a confirmed digital_logic spec + topology_selections + a *converged*
digital_sizings row (the DUT) directly via SQL.

Skipped automatically when:
  * symbiyosys sidecar unreachable, OR
  * Ollama unreachable, OR
  * SCAFFOLD_SKIP_LIVE_LLM=1.

Tolerates legitimate non-convergence — proving an LLM-authored DUT+SVA harness
is genuinely hard, and may not PASS within the iteration budget. The test
asserts the §17.414 audit-the-attempt invariant: a ``formal_verifications`` row
is persisted regardless, and its ``sim_run_ids`` link to ``sim_runs`` rows with
``tool='symbiyosys'``.
"""
from __future__ import annotations

import json
import os
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.sim.formal_verify import verify_design
from app.sim.symbiyosys import VALID_VERDICTS

_DIGITAL_SPEC: dict = {
    "schema_version": "1.0.0",
    "design": {
        "name": "N-bit counter (live formal-verify integration)",
        "kind": "digital_logic",
        "description": (
            "Synchronous counter that wraps at 2^N. Formal property: the "
            "count never exceeds 2^WIDTH-1."
        ),
    },
    "constraints": [
        {
            "id": "no_overflow",
            "kind": "timing.latency",
            "description": "count is always <= 2^WIDTH-1 (never overflows).",
            "max": 15.0,
            "unit": "cycles",
            "criticality": "required",
        }
    ],
}

_COUNTER_CANDIDATE: dict = {
    "name": "Synchronous wrap counter",
    "description": "Free-running 4-bit counter that wraps at 16.",
    "rationale": "Simplest digital design with a provable overflow bound.",
    "citations": ["chunk-digital-A"],
}

# A simple converged DUT seed — the LLM strips this into a formal-clean module
# and authors an SVA harness asserting the no-overflow property.
_DUT_SEED = (
    "module counter #(parameter WIDTH = 4) (\n"
    "  input  logic clk,\n"
    "  input  logic rst_n,\n"
    "  output logic [WIDTH-1:0] count\n"
    ");\n"
    "  always_ff @(posedge clk or negedge rst_n) begin\n"
    "    if (!rst_n) count <= '0;\n"
    "    else        count <= count + 1'b1;\n"
    "  end\n"
    "endmodule\n"
)


@pytest_asyncio.fixture
async def converged_digital_setup():
    """Insert confirmed digital_logic spec + topology_selections + a converged
    digital_sizings row; teardown CASCADEs through to formal_verifications."""
    spec_ids: list[str] = []

    async def _setup() -> tuple[str, str, str]:
        async with async_session() as db:
            spec_row = await db.execute(
                text(
                    """
                    INSERT INTO specs (
                        schema_version, spec_json, spec_sha256,
                        confirmed_by, confirmed_at
                    )
                    VALUES (
                        '1.0.0', CAST(:sj AS JSONB), :sh, 'api_key', NOW()
                    )
                    RETURNING id
                    """
                ),
                {
                    "sj": json.dumps(_DIGITAL_SPEC),
                    "sh": f"formal-verify-{uuid.uuid4().hex[:16]}",
                },
            )
            spec_id = str(spec_row.scalar_one())

            sel_row = await db.execute(
                text(
                    """
                    INSERT INTO topology_selections (
                        spec_id, candidates, rag_chunk_ids,
                        rag_query, rag_domain, model_used
                    )
                    VALUES (
                        :spec_id, CAST(:cands AS JSONB), :chunks,
                        'integration test fixture', 'eng_design', 'test-fixture'
                    )
                    RETURNING id
                    """
                ),
                {
                    "spec_id": spec_id,
                    "cands": json.dumps([_COUNTER_CANDIDATE]),
                    "chunks": ["chunk-digital-A"],
                },
            )
            sel_id = str(sel_row.scalar_one())

            ds_row = await db.execute(
                text(
                    """
                    INSERT INTO digital_sizings (
                        spec_id, topology_selection_id, candidate_idx,
                        final_sv_source, top_module, converged, iterations,
                        model_used
                    )
                    VALUES (
                        :spec_id, :sel_id, 0, :sv, 'tb', TRUE, 1,
                        'test-fixture'
                    )
                    RETURNING id
                    """
                ),
                {"spec_id": spec_id, "sel_id": sel_id, "sv": _DUT_SEED},
            )
            ds_id = str(ds_row.scalar_one())
            await db.commit()

        spec_ids.append(spec_id)
        return spec_id, sel_id, ds_id

    yield _setup

    if spec_ids:
        async with async_session() as db:
            await db.execute(
                text("DELETE FROM specs WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": spec_ids},
            )
            await db.commit()


async def _services_ready() -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{settings.symbiyosys_url}/health")
            r.raise_for_status()
            if not r.json().get("ok"):
                return False, "symbiyosys sidecar reports not-ok"
    except Exception as e:
        return False, f"symbiyosys unreachable: {e}"
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{settings.ollama_base_url}/api/tags")
            r.raise_for_status()
    except Exception as e:
        return False, f"ollama unreachable: {e}"
    return True, ""


# §17.335 — per-test timeout override (suite-wide `--timeout=30` from
# `make test` would pre-empt the LLM→sby repair loop otherwise).
@pytest.mark.smoke
@pytest.mark.timeout(900)
async def test_formal_verify_live_end_to_end(converged_digital_setup):
    if os.environ.get("SCAFFOLD_SKIP_LIVE_LLM") == "1":
        pytest.skip("SCAFFOLD_SKIP_LIVE_LLM=1")
    ready, reason = await _services_ready()
    if not ready:
        pytest.skip(reason)

    spec_id, sel_id, ds_id = await converged_digital_setup()

    async with async_session() as db:
        result = await verify_design(
            uuid.UUID(ds_id), db=db, max_iterations=2,
        )

    # Audit-the-attempt invariant: a row is persisted regardless of verdict.
    assert result.formal_verification_id is not None
    assert result.spec_id == uuid.UUID(spec_id)
    assert result.topology_selection_id == uuid.UUID(sel_id)
    assert result.digital_sizing_id == uuid.UUID(ds_id)
    assert result.iterations >= 1
    assert result.top_module == "formal_top"
    # verdict is one of the valid sby verdicts (or None only if no attempt ran,
    # which shouldn't happen with a reachable sidecar).
    assert result.verdict is None or result.verdict in VALID_VERDICTS

    async with async_session() as db:
        row = await db.execute(
            text(
                """
                SELECT id, verdict, converged, mode, iterations,
                       array_length(sim_run_ids, 1) AS n_sims
                FROM formal_verifications
                WHERE id = :id
                """
            ),
            {"id": str(result.formal_verification_id)},
        )
        persisted = row.mappings().one()
        assert persisted["iterations"] == result.iterations
        assert persisted["mode"] == settings.formal_verify_mode

        # sim_run_ids link to symbiyosys attestations (§17.142 audit invariant).
        if persisted["n_sims"]:
            tools = await db.execute(
                text(
                    """
                    SELECT DISTINCT tool FROM sim_runs
                    WHERE id = ANY(
                        SELECT unnest(sim_run_ids) FROM formal_verifications
                        WHERE id = :id
                    )
                    """
                ),
                {"id": str(result.formal_verification_id)},
            )
            assert {t[0] for t in tools.all()} == {"symbiyosys"}

    if not result.converged:
        print(
            f"WARNING: live formal verification did not prove "
            f"(verdict={result.verdict}, iterations={result.iterations}, "
            f"errors={result.errors}) — audit row persisted at "
            f"id={result.formal_verification_id}"
        )
