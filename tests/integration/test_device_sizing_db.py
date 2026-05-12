"""
Integration test for /topology-selections/{id}/size — exercises the
full closed loop against the live ngspice sidecar and the real LLM.

Avoids the live-RAG dependency (the §17.146 integration test
demonstrated the corpus is currently anthropic-SDK chunks): the test
inserts a confirmed spec AND a hand-crafted topology_selections row
directly via SQL. The selection's ``candidates`` field carries one
RC-low-pass candidate; the LLM only needs to produce a netlist that
implements it.

Skipped automatically when:
  * ngspice sidecar unreachable, OR
  * Ollama unreachable, OR
  * SCAFFOLD_SKIP_LIVE_LLM=1.

Tolerates legitimate non-convergence (LLM-emitted SPICE may have
syntax glitches the §17.140 wrapper catches) — the test asserts that
a device_sizings row is persisted regardless, which is the §17.147
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

_VALID_SPEC: dict = {
    "schema_version": "1.0.0",
    "design": {
        "name": "RC low-pass (live sizing integration)",
        "kind": "analog_circuit",
        "description": "First-order passive RC low-pass for audio band.",
    },
    "constraints": [
        {
            "id": "fc_3db",
            "kind": "electrical.frequency",
            "description": "-3 dB corner frequency.",
            "target": 1000.0,
            "tolerance_pct": 10.0,
            "unit": "Hz",
            "criticality": "required",
        },
    ],
}

_RC_CANDIDATE: dict = {
    "name": "First-order RC low-pass",
    "description": "Single resistor R1 from input to output, capacitor C1 from output to ground.",
    "rationale": "Simplest passive realization for the requested corner frequency.",
    "citations": ["chunk-A"],
}


@pytest_asyncio.fixture
async def confirmed_spec_and_selection():
    """Insert one confirmed spec + one topology_selections row; clean
    up afterwards (cascade also drops any device_sizings)."""
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
                    "sj": json.dumps(_VALID_SPEC),
                    "sh": f"sizing-test-{uuid.uuid4().hex[:16]}",
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
                        'hand-crafted for live sizing test',
                        'eng', 'test-fixture'
                    )
                    RETURNING id
                    """
                ),
                {
                    "spec_id": spec_id,
                    "cands": json.dumps([_RC_CANDIDATE]),
                    "chunks": ["chunk-A"],
                },
            )
            sel_id = str(sel_row.scalar_one())
            await db.commit()

        spec_ids.append(spec_id)
        return spec_id, sel_id

    yield _setup

    if spec_ids:
        async with async_session() as db:
            # CASCADE deletes selections → device_sizings.
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
            r = await c.get(f"{settings.ngspice_url}/health")
            r.raise_for_status()
            if not r.json().get("ok"):
                return False, "ngspice sidecar reports not-ok"
    except Exception as e:
        return False, f"ngspice unreachable: {e}"
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{settings.ollama_base_url}/api/tags")
            r.raise_for_status()
    except Exception as e:
        return False, f"ollama unreachable: {e}"
    return True, ""


@pytest.mark.smoke
async def test_device_sizing_live_end_to_end(confirmed_spec_and_selection):
    if os.environ.get("SCAFFOLD_SKIP_LIVE_LLM") == "1":
        pytest.skip("SCAFFOLD_SKIP_LIVE_LLM=1")
    ready, reason = await _services_ready()
    if not ready:
        pytest.skip(reason)

    spec_id, sel_id = await confirmed_spec_and_selection()

    # The closed loop may chew through up to 3 LLM rounds × ngspice
    # build/run. Give the orchestrator round-trip plenty of headroom.
    async with httpx.AsyncClient(timeout=1500.0) as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/topology-selections/{sel_id}/size",
            params={"max_iterations": 3},
            headers=_api_headers(),
        )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["spec_id"] == spec_id
    assert body["topology_selection_id"] == sel_id
    assert body["candidate_idx"] == 0
    assert body["iterations"] >= 1

    # Row is persisted regardless of convergence — the §17.147
    # audit-the-attempt invariant.
    async with async_session() as db:
        row = await db.execute(
            text(
                """
                SELECT id, converged, iterations,
                       array_length(sim_run_ids, 1) AS n_sims
                FROM device_sizings
                WHERE id = :id
                """
            ),
            {"id": body["id"]},
        )
        persisted = row.mappings().one()
    assert persisted["iterations"] == body["iterations"]

    # On the cloud 235b the loop very often converges on iter 1 (the
    # RC LPF analytical relation is trivial). If it doesn't, the test
    # still passes — the audit row is there. Print enough diagnostic
    # so a regression that drops convergence is visible.
    if not body["converged"]:
        print(
            f"WARNING: live sizing did not converge (iterations="
            f"{body['iterations']}, errors={body['errors']}) — audit "
            f"row persisted at id={body['id']}"
        )
    else:
        assert "fc_3db" in body["final_measurements"]
        measured_fc = body["final_measurements"]["fc_3db"]
        # Loose check — we set tolerance_pct=10 on the constraint,
        # so anything in [900, 1100] should have been accepted.
        assert 800.0 <= measured_fc <= 1200.0, (
            f"unexpected converged fc: {measured_fc}"
        )
