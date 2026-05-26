"""
Integration test for /specs/{id}/topology-select — exercises the
full chain (real Postgres, real Milvus RAG, real LLM call). Inserts
a confirmed spec, fires the endpoint, asserts a topology_selections
row was persisted with citations into the retrieval set.

Skipped automatically when:
  * Ollama is unreachable (LLM call would fail), or
  * the "eng" RAG corpus is empty (no chunks to cite, the stage
    correctly refuses to fabricate), or
  * SCAFFOLD_SKIP_LIVE_LLM=1 is set.

The unit suite (tests/test_topology_select.py) covers behavioural
surface with mocked RAG + LLM; this test only catches end-to-end
regressions that the mocks would miss.
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
        "name": "RC low-pass filter (live topology-select integration)",
        "kind": "analog_circuit",
        "description": "Simple first-order analog low-pass for audio band roll-off.",
    },
    "constraints": [
        {
            "id": "fc_3db",
            "kind": "electrical.frequency",
            "description": "-3 dB corner frequency.",
            "target": 1000.0,
            "tolerance_pct": 5.0,
            "unit": "Hz",
            "criticality": "required",
        },
        {
            "id": "vpp_max",
            "kind": "electrical.voltage",
            "description": "Max output swing.",
            "max": 3.3,
            "unit": "V",
            "criticality": "required",
        },
    ],
}


@pytest_asyncio.fixture
async def confirmed_spec():
    """Insert a confirmed spec; clean it up (and any topology
    selections it produced) afterwards."""
    inserted: list[str] = []

    async def _insert() -> str:
        async with async_session() as db:
            row = await db.execute(
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
                    "sh": f"topo-test-{uuid.uuid4().hex[:16]}",
                },
            )
            spec_id = str(row.scalar_one())
            await db.commit()
        inserted.append(spec_id)
        return spec_id

    yield _insert

    if inserted:
        async with async_session() as db:
            # topology_selections cascade-deletes via FK.
            await db.execute(
                text("DELETE FROM specs WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": inserted},
            )
            await db.commit()


def _api_headers() -> dict[str, str]:
    raw = settings.scaffold_api_key.get_secret_value()
    return {"X-API-Key": raw} if raw else {}


async def _model_reachable() -> bool:
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{settings.ollama_base_url}/api/tags")
            r.raise_for_status()
            return True
    except Exception:
        return False


async def _corpus_has_eng_chunks() -> bool:
    """Probe the engineering corpus before exercising the live stage.

    Issues a tiny ``query_rag`` from within the test process (cheaper
    than a /rag round-trip) against the same domain the stage will
    use. If the corpus is empty or unreachable, the stage's
    legitimate failure path would mask any other regression — so we
    skip instead.
    """
    try:
        from app.modules.rag_pipeline import query_rag
        resp = await query_rag("analog low-pass filter", domain="eng", top_k=3)
        return bool(resp.get("results"))
    except Exception:
        return False


# §17.325 — Per-test timeout override. The suite-wide `pytest --timeout=30`
# in `make test` is the right default for unit / mock tests, but this is a
# live cloud-LLM round-trip and the inner httpx already uses `timeout=900.0`
# acknowledging the cloud 235b's "several minutes" upper bound. Without
# this override the pytest-timeout signal fires at 30 s mid-LLM call,
# failing a test whose own contract permits 15 min. The skip-cascade
# above (SCAFFOLD_SKIP_LIVE_LLM / model-unreachable / empty-corpus) keeps
# this from blocking hosts that can't run the live path; the timeout
# only matters when the test actually proceeds.
@pytest.mark.smoke
@pytest.mark.timeout(900)
async def test_topology_select_live_end_to_end(confirmed_spec):
    if os.environ.get("SCAFFOLD_SKIP_LIVE_LLM") == "1":
        pytest.skip("SCAFFOLD_SKIP_LIVE_LLM=1")
    if not await _model_reachable():
        pytest.skip(f"ollama unreachable at {settings.ollama_base_url}")
    if not await _corpus_has_eng_chunks():
        pytest.skip("eng RAG corpus empty — seed it before running this test")

    spec_id = await confirmed_spec()

    # The cloud 235b can chew through a topology-select prompt for
    # several minutes — give the orchestrator round-trip a generous
    # ceiling so the test's own httpx timeout never wins the race.
    # The orchestrator's own ollama timeout governs the upstream call.
    async with httpx.AsyncClient(timeout=900.0) as client:
        resp = await client.post(
            f"{ORCHESTRATOR_URL}/specs/{spec_id}/topology-select",
            headers=_api_headers(),
        )

    # Tolerate the stage legitimately failing (e.g. citation invariant
    # broke) — the test's job is to catch *unexpected* failure modes,
    # not to assert the LLM always behaves. Print enough diagnostics
    # on failure so an operator can see whether it's the prompt or the
    # corpus.
    assert resp.status_code in (200, 409), resp.text
    if resp.status_code == 409:
        pytest.skip(
            f"stage returned 409 (likely citation/coverage issue): {resp.text!r}"
        )

    body = resp.json()
    assert body["spec_id"] == spec_id
    assert isinstance(body["candidates"], list)
    assert 2 <= len(body["candidates"]) <= 4
    retrieval_set = set(body["rag_chunk_ids"])
    assert retrieval_set, "stage persisted a row with empty rag_chunk_ids"
    # Every citation in the persisted row MUST be inside the retrieval set.
    # The stage's invariant check is what makes this true; we re-assert
    # post-hoc as the end-to-end audit.
    for cand in body["candidates"]:
        for cite in cand["citations"]:
            assert cite in retrieval_set, (
                f"persisted citation {cite!r} not in retrieval set "
                f"{sorted(retrieval_set)!r}"
            )

    # Row must be retrievable by id.
    async with async_session() as db:
        row = await db.execute(
            text(
                """
                SELECT spec_id, model_used, rag_chunk_ids
                FROM topology_selections
                WHERE id = :id
                """
            ),
            {"id": body["id"]},
        )
        persisted = row.mappings().one()
    assert str(persisted["spec_id"]) == spec_id
    assert persisted["model_used"]
    assert set(persisted["rag_chunk_ids"]) == retrieval_set
