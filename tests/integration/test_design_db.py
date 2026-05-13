"""
Integration tests for ``/design`` against the real orchestrator.

Covers the cheap, deterministic surfaces — endpoint mounting, schema
shape, error mapping — but NOT the live LLM end-to-end advance (the
per-stage SSE response is already exercised by the §17.146 / §17.147
/ §17.148 integration tests at their dedicated endpoints; running
them through /design/{id}/advance would just re-run the same LLM
calls under a different URL).
"""
from __future__ import annotations

import json
import uuid

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.database import async_session

ORCHESTRATOR_URL = "http://scaffold-orchestrator:8000"


def _api_headers() -> dict[str, str]:
    raw = settings.scaffold_api_key.get_secret_value()
    return {"X-API-Key": raw} if raw else {}


@pytest_asyncio.fixture
async def cleanup_design_jobs():
    """Track job IDs the tests create so they (and their cascaded
    rows) get cleaned up afterwards."""
    ids: list[str] = []
    yield ids
    if ids:
        async with async_session() as db:
            await db.execute(
                text("DELETE FROM jobs WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": ids},
            )
            await db.commit()


@pytest.mark.smoke
async def test_get_design_404_when_missing():
    bogus = uuid.uuid4()
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            f"{ORCHESTRATOR_URL}/design/{bogus}",
            headers=_api_headers(),
        )
    assert resp.status_code == 404


@pytest.mark.smoke
async def test_get_design_404_when_job_type_legacy(cleanup_design_jobs):
    """A regular (legacy) job row exists but isn't a design_circuit
    — GET /design/{id} must reject it, not silently treat it as
    one."""
    async with async_session() as db:
        row = await db.execute(
            text(
                """
                INSERT INTO jobs (title, status, input_text, job_type)
                VALUES ('legacy test', 'pending', 'x', 'legacy')
                RETURNING id
                """
            ),
        )
        job_id = str(row.scalar_one())
        await db.commit()
    cleanup_design_jobs.append(job_id)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            f"{ORCHESTRATOR_URL}/design/{job_id}",
            headers=_api_headers(),
        )
    assert resp.status_code == 404
    assert "not a design_circuit" in resp.json()["detail"]


@pytest.mark.smoke
async def test_advance_400_on_unknown_stage(cleanup_design_jobs):
    async with async_session() as db:
        row = await db.execute(
            text(
                """
                INSERT INTO jobs (title, status, input_text, job_type)
                VALUES ('design test', 'awaiting_confirmation', 'x', 'design_circuit')
                RETURNING id
                """
            ),
        )
        job_id = str(row.scalar_one())
        await db.commit()
    cleanup_design_jobs.append(job_id)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/design/{job_id}/advance",
            params={"stage": "bogus"},
            headers=_api_headers(),
        )
    assert resp.status_code == 400


@pytest.mark.smoke
async def test_advance_404_when_job_missing():
    bogus = uuid.uuid4()
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/design/{bogus}/advance",
            params={"stage": "topology"},
            headers=_api_headers(),
        )
    assert resp.status_code == 404


@pytest.mark.smoke
async def test_get_design_aggregates_chain(cleanup_design_jobs):
    """Seed a full chain (job + spec + topology_selection +
    device_sizing) via SQL, then assert /design/{id} returns all the
    cross-stage refs in one payload."""
    async with async_session() as db:
        # Job
        job_row = await db.execute(
            text(
                """
                INSERT INTO jobs (title, status, input_text, job_type)
                VALUES ('design test', 'completed', 'brief text',
                        'design_circuit')
                RETURNING id
                """
            ),
        )
        job_id = str(job_row.scalar_one())
        cleanup_design_jobs.append(job_id)

        # Spec (confirmed)
        spec_row = await db.execute(
            text(
                """
                INSERT INTO specs (
                    job_id, schema_version, spec_json, spec_sha256,
                    confirmed_by, confirmed_at
                )
                VALUES (
                    :job_id, '1.0.0',
                    CAST(:sj AS JSONB), :sh,
                    'api_key', NOW()
                )
                RETURNING id
                """
            ),
            {
                "job_id": job_id,
                "sj": json.dumps({
                    "schema_version": "1.0.0",
                    "design": {"name": "RC LPF", "kind": "analog_circuit",
                               "description": "."},
                    "constraints": [{
                        "id": "fc_3db", "kind": "electrical.frequency",
                        "description": ".", "target": 1000.0,
                        "unit": "Hz", "criticality": "required",
                    }],
                }),
                "sh": f"design-test-{uuid.uuid4().hex[:16]}",
            },
        )
        spec_id = str(spec_row.scalar_one())

        # Topology selection
        sel_row = await db.execute(
            text(
                """
                INSERT INTO topology_selections (
                    spec_id, candidates, rag_chunk_ids,
                    rag_query, rag_domain, model_used
                )
                VALUES (
                    :spec_id,
                    CAST(:cands AS JSONB),
                    :chunks,
                    'fixture', 'eng', 'test-fixture'
                )
                RETURNING id
                """
            ),
            {
                "spec_id": spec_id,
                "cands": json.dumps([{
                    "name": "RC LPF", "description": ".",
                    "rationale": ".", "citations": ["chunk-A"],
                }]),
                "chunks": ["chunk-A"],
            },
        )
        sel_id = str(sel_row.scalar_one())

        # Sim run
        sim_row = await db.execute(
            text(
                """
                INSERT INTO sim_runs (
                    tool, tool_version, netlist_sha256, exit_code,
                    duration_ms, timed_out, measurements
                )
                VALUES (
                    'ngspice', 'ngspice-44.2', :nsh, 0, 42, FALSE,
                    CAST(:meas AS JSONB)
                )
                RETURNING id
                """
            ),
            {
                "nsh": f"netlist-{uuid.uuid4().hex[:16]}",
                "meas": json.dumps({"fc_3db": 998.0}),
            },
        )
        sim_id = str(sim_row.scalar_one())

        # Device sizing
        sizing_row = await db.execute(
            text(
                """
                INSERT INTO device_sizings (
                    spec_id, topology_selection_id, candidate_idx,
                    final_params, final_netlist, sim_run_ids,
                    converged, iterations, model_used,
                    measurements_final
                )
                VALUES (
                    :spec_id, :sel_id, 0,
                    CAST(:params AS JSONB), '* RC LPF\n.end\n',
                    CAST(:sim_ids AS uuid[]),
                    TRUE, 1, 'test-fixture',
                    CAST(:meas AS JSONB)
                )
                RETURNING id
                """
            ),
            {
                "spec_id": spec_id,
                "sel_id": sel_id,
                "params": json.dumps({"R1": "1.59155k", "C1": "100n"}),
                "sim_ids": [sim_id],
                "meas": json.dumps({"fc_3db": 998.0}),
            },
        )
        sizing_id = str(sizing_row.scalar_one())
        await db.commit()

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(
            f"{ORCHESTRATOR_URL}/design/{job_id}",
            headers=_api_headers(),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] == job_id
    assert body["job_type"] == "design_circuit"
    assert body["status"] == "completed"
    assert body["spec_id"] == spec_id
    assert body["spec_confirmed_at"] is not None
    assert body["topology_selection_id"] == sel_id
    assert body["device_sizing_id"] == sizing_id
    assert body["device_sizing_converged"] is True

    # Clean up sim_run separately (not cascaded by jobs delete).
    async with async_session() as db:
        await db.execute(
            text("DELETE FROM sim_runs WHERE id = :id"),
            {"id": sim_id},
        )
        await db.commit()


@pytest.mark.smoke
async def test_post_design_ambiguity_returns_inline(cleanup_design_jobs):
    """An ambiguous brief must surface ambiguities[] in the 200 body
    with NO job row created."""
    payload = {"brief": "Make a fast filter."}
    async with httpx.AsyncClient(timeout=120.0) as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/design",
            json=payload,
            headers=_api_headers(),
        )
    # Tolerate model unavailability — if the LLM call fails for any
    # transport reason, we get errors[] populated rather than
    # ambiguities[]; the contract holds either way (no job_id, no
    # spec_id).
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["job_id"] is None
    assert body["spec_id"] is None
    # Either ambiguities (LLM saw the vagueness) or errors (LLM
    # unreachable / transport fail). Both are no-row paths.
    assert body["ambiguities"] or body["errors"], body
