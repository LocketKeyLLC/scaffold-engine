"""
Integration tests for GET /device-sizings/{id}/report against real
Postgres. Inserts a complete pipeline state (spec → selection →
sizing → sim_run) via SQL and verifies the report joins everything
correctly + the Markdown rendering contains the expected sections.

No live LLM / RAG / sidecars required — the report stage is a pure
DB projection (Milvus fetch is best-effort and skipped gracefully
when the corpus has no matching entry_ids).
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


_SPEC_JSON = {
    "schema_version": "1.0.0",
    "design": {
        "name": "RC LPF (live report integration)",
        "kind": "analog_circuit",
        "description": "First-order passive RC low-pass.",
    },
    "constraints": [
        {
            "id": "fc_3db",
            "kind": "electrical.frequency",
            "description": "-3 dB corner.",
            "target": 1000.0,
            "tolerance_pct": 5.0,
            "unit": "Hz",
            "criticality": "required",
        }
    ],
}

_CANDIDATE = {
    "name": "RC low-pass",
    "description": "Resistor + capacitor.",
    "rationale": "Trivial fit.",
    "citations": ["chunk-A-report-test"],  # likely missing from corpus
}


def _api_headers() -> dict[str, str]:
    raw = settings.scaffold_api_key.get_secret_value()
    return {"X-API-Key": raw} if raw else {}


@pytest_asyncio.fixture
async def seeded_pipeline():
    """Insert a full pipeline chain: spec → topology_selection →
    sim_runs → device_sizings. Cascade-cleans afterwards."""
    spec_ids: list[str] = []
    sim_run_ids: list[str] = []

    async def _seed(*, converged: bool = True, measured_fc: float = 1003.5):
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
                    "sj": json.dumps(_SPEC_JSON),
                    "sh": f"report-test-{uuid.uuid4().hex[:16]}",
                },
            )
            spec_id = str(spec_row.scalar_one())
            spec_ids.append(spec_id)

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
                    "cands": json.dumps([_CANDIDATE]),
                    "chunks": ["chunk-A-report-test"],
                },
            )
            sel_id = str(sel_row.scalar_one())

            sim_row = await db.execute(
                text(
                    """
                    INSERT INTO sim_runs (
                        tool, tool_version, netlist_sha256, exit_code,
                        stdout, stderr, measurements, duration_ms,
                        timed_out
                    )
                    VALUES (
                        'ngspice', 'ngspice-44.2', :nsh, 0,
                        '', '', CAST(:meas AS JSONB), 42, FALSE
                    )
                    RETURNING id
                    """
                ),
                {
                    "nsh": f"netlist-{uuid.uuid4().hex[:16]}",
                    "meas": json.dumps({"fc_3db": measured_fc}),
                },
            )
            sim_run_id = str(sim_row.scalar_one())
            sim_run_ids.append(sim_run_id)

            sizing_row = await db.execute(
                text(
                    """
                    INSERT INTO device_sizings (
                        spec_id, topology_selection_id, candidate_idx,
                        final_params, final_netlist, sim_run_ids,
                        converged, iterations, model_used,
                        measurements_final, errors
                    )
                    VALUES (
                        :spec_id, :sel_id, 0,
                        CAST(:params AS JSONB),
                        '* RC LPF\nV1 in 0 AC 1\n.end\n',
                        CAST(:sim_run_ids AS uuid[]),
                        :converged, 1, 'test-fixture',
                        CAST(:meas AS JSONB), :errs
                    )
                    RETURNING id
                    """
                ),
                {
                    "spec_id": spec_id,
                    "sel_id": sel_id,
                    "params": json.dumps({"R1": "1k", "C1": "159.155n"}),
                    "sim_run_ids": [sim_run_id],
                    "converged": converged,
                    "meas": json.dumps({"fc_3db": measured_fc}),
                    "errs": [] if converged else ["budget exhausted"],
                },
            )
            sizing_id = str(sizing_row.scalar_one())
            await db.commit()
        return spec_id, sel_id, sizing_id, sim_run_id

    yield _seed

    async with async_session() as db:
        if spec_ids:
            # specs CASCADE → topology_selections → device_sizings.
            await db.execute(
                text("DELETE FROM specs WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": spec_ids},
            )
        if sim_run_ids:
            await db.execute(
                text("DELETE FROM sim_runs WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": sim_run_ids},
            )
        await db.commit()


@pytest.mark.smoke
async def test_get_report_json_converged(seeded_pipeline):
    _, _, sizing_id, sim_run_id = await seeded_pipeline(converged=True)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{ORCHESTRATOR_URL}/device-sizings/{sizing_id}/report",
            headers=_api_headers(),
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["sizing_id"] == sizing_id
    assert body["converged"] is True
    assert body["iterations"] == 1
    assert body["design_name"] == "RC LPF (live report integration)"
    assert len(body["constraints"]) == 1
    cstrnt = body["constraints"][0]
    assert cstrnt["id"] == "fc_3db"
    assert cstrnt["status"] == "ok"
    assert cstrnt["measured"] == 1003.5
    # Sized params round-tripped from JSONB.
    assert body["final_params"] == {"R1": "1k", "C1": "159.155n"}
    # Sim run manifest joined by sim_run_ids[].
    assert len(body["sim_runs"]) == 1
    assert body["sim_runs"][0]["sim_run_id"] == sim_run_id
    assert body["sim_runs"][0]["tool"] == "ngspice"
    # Citation references a chunk that doesn't exist in the live
    # corpus — must render as unavailable, not fail.
    assert len(body["citations"]) == 1
    assert body["citations"][0]["available"] is False


@pytest.mark.smoke
async def test_get_report_markdown_format(seeded_pipeline):
    _, _, sizing_id, _ = await seeded_pipeline(converged=True)

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{ORCHESTRATOR_URL}/device-sizings/{sizing_id}/report",
            params={"format": "markdown"},
            headers=_api_headers(),
        )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/markdown")
    md = resp.text
    assert "# RC LPF (live report integration)" in md
    assert "## Spec" in md
    assert "## Topology" in md
    assert "## Sized Parameters" in md
    assert "## Sim Run Manifest" in md
    assert "`fc_3db`" in md
    assert "ngspice-44.2" in md
    # No NOT CONVERGED banner on a converged sizing.
    assert "NOT CONVERGED" not in md


@pytest.mark.smoke
async def test_get_report_non_converged_renders_with_banner(seeded_pipeline):
    _, _, sizing_id, _ = await seeded_pipeline(
        converged=False, measured_fc=5000.0,
    )

    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{ORCHESTRATOR_URL}/device-sizings/{sizing_id}/report",
            params={"format": "markdown"},
            headers=_api_headers(),
        )
    assert resp.status_code == 200
    md = resp.text
    assert "NOT CONVERGED" in md
    assert "## Audit — Diagnostics" in md
    assert "budget exhausted" in md


@pytest.mark.smoke
async def test_get_report_404_when_sizing_missing():
    bogus = uuid.uuid4()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{ORCHESTRATOR_URL}/device-sizings/{bogus}/report",
            headers=_api_headers(),
        )
    assert resp.status_code == 404


@pytest.mark.smoke
async def test_get_report_400_on_unknown_format(seeded_pipeline):
    _, _, sizing_id, _ = await seeded_pipeline()
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.get(
            f"{ORCHESTRATOR_URL}/device-sizings/{sizing_id}/report",
            params={"format": "pdf"},
            headers=_api_headers(),
        )
    assert resp.status_code == 400
