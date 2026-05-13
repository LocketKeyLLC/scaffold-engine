"""
Integration test for /topology-selections/{id}/size when
``design.kind == 'digital_logic'`` (§17.152). Exercises the full
chain: real Postgres, real Verilator sidecar, real cloud LLM.

Avoids the live-RAG dependency by seeding a confirmed digital_logic
spec AND a hand-crafted topology_selections row directly via SQL —
same shape as §17.147's analog integration test.

Skipped automatically when:
  * Verilator sidecar unreachable, OR
  * Ollama unreachable, OR
  * SCAFFOLD_SKIP_LIVE_LLM=1.

Tolerates legitimate non-convergence (LLM-emitted SV may have syntax
glitches the §17.141 wrapper catches) — the test asserts that a
``digital_sizings`` row is persisted regardless, which is the §17.152
audit-the-attempt invariant.
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

ORCHESTRATOR_URL = "http://scaffold-orchestrator:8000"

_DIGITAL_SPEC: dict = {
    "schema_version": "1.0.0",
    "design": {
        "name": "N-bit counter (live digital sizing integration)",
        "kind": "digital_logic",
        "description": (
            "Synchronous counter that wraps at 2^N. Testbench measures "
            "cycles to first wrap."
        ),
    },
    "constraints": [
        {
            "id": "wrap_count",
            "kind": "timing.latency",
            "description": "Cycles between resets to first wrap.",
            "target": 16.0,
            "tolerance_pct": 5.0,
            "unit": "cycles",
            "criticality": "required",
        }
    ],
}

_COUNTER_CANDIDATE: dict = {
    "name": "Synchronous wrap counter",
    "description": "Free-running counter that wraps at 2^N.",
    "rationale": "Simplest possible digital design for the target wrap count.",
    "citations": ["chunk-digital-A"],
}


@pytest_asyncio.fixture
async def confirmed_digital_setup():
    """Insert confirmed digital_logic spec + topology_selections row;
    teardown CASCADEs through to digital_sizings."""
    spec_ids: list[str] = []

    async def _setup() -> tuple[str, str]:
        async with async_session() as db:
            spec_row = await db.execute(
                text(
                    """
                    INSERT INTO specs (
                        schema_version, spec_json, spec_sha256,
                        confirmed_by, confirmed_at
                    )
                    VALUES (
                        '1.0.0', CAST(:sj AS JSONB), :sh,
                        'api_key', NOW()
                    )
                    RETURNING id
                    """
                ),
                {
                    "sj": json.dumps(_DIGITAL_SPEC),
                    "sh": f"digital-sizing-{uuid.uuid4().hex[:16]}",
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
                        'integration test fixture', 'eng',
                        'test-fixture'
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
            await db.commit()

        spec_ids.append(spec_id)
        return spec_id, sel_id

    yield _setup

    if spec_ids:
        async with async_session() as db:
            await db.execute(
                text("DELETE FROM specs WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": spec_ids},
            )
            await db.commit()


def _api_headers() -> dict[str, str]:
    raw = settings.scaffold_api_key.get_secret_value()
    return {"X-API-Key": raw} if raw else {}


async def _services_ready() -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{settings.verilator_url}/health")
            r.raise_for_status()
            if not r.json().get("ok"):
                return False, "verilator sidecar reports not-ok"
    except Exception as e:
        return False, f"verilator unreachable: {e}"
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{settings.ollama_base_url}/api/tags")
            r.raise_for_status()
    except Exception as e:
        return False, f"ollama unreachable: {e}"
    return True, ""


@pytest.mark.smoke
async def test_digital_sizing_live_end_to_end(confirmed_digital_setup):
    if os.environ.get("SCAFFOLD_SKIP_LIVE_LLM") == "1":
        pytest.skip("SCAFFOLD_SKIP_LIVE_LLM=1")
    ready, reason = await _services_ready()
    if not ready:
        pytest.skip(reason)

    spec_id, sel_id = await confirmed_digital_setup()

    # Verilator builds + runs can take a while; give plenty of headroom.
    async with httpx.AsyncClient(timeout=1500.0) as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/topology-selections/{sel_id}/size",
            params={"max_iterations": 3},
            headers=_api_headers(),
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "digital"
    assert body["spec_id"] == spec_id
    assert body["topology_selection_id"] == sel_id
    assert body["candidate_idx"] == 0
    assert body["iterations"] >= 1
    assert body["top_module"] == "tb"

    # Row persisted regardless of convergence — the §17.152 audit-the-
    # attempt invariant.
    async with async_session() as db:
        row = await db.execute(
            text(
                """
                SELECT id, converged, iterations, top_module,
                       array_length(sim_run_ids, 1) AS n_sims
                FROM digital_sizings
                WHERE id = :id
                """
            ),
            {"id": body["id"]},
        )
        persisted = row.mappings().one()
    assert persisted["iterations"] == body["iterations"]
    assert persisted["top_module"] == "tb"

    if not body["converged"]:
        print(
            f"WARNING: live digital sizing did not converge "
            f"(iterations={body['iterations']}, errors={body['errors']}) "
            f"— audit row persisted at id={body['id']}"
        )
    else:
        assert "wrap_count" in body["final_measurements"]
        wrap = body["final_measurements"]["wrap_count"]
        # tolerance ±5% on target 16
        assert 15 <= wrap <= 17, (
            f"unexpected converged wrap_count: {wrap}"
        )
