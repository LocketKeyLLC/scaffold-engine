"""
Integration tests for Scaffold Engine live services.

Marker: @pytest.mark.validate
Run:    docker exec scaffold-orchestrator make -C /app validate

Requirements:
  - FastAPI app running (scaffold-orchestrator container)
  - Ollama reachable at 172.18.0.1:11434
  - Milvus loaded with technical_knowledge collection (83 entries)
  - PostgreSQL scaffold_engine DB accessible
"""

import pytest
import httpx
import asyncio
import pytest_asyncio
import os

# §17.828 (plan 7.5) — live-stack suite (Ollama + Milvus + Postgres); the
# `integration` marker keeps it deselected in cloud CI's `-m "not integration"`
# run, exactly as the old `-k "not integration"` name-substring match did.
pytestmark = pytest.mark.integration

SCAFFOLD_API_KEY = os.environ.get("SCAFFOLD_API_KEY", "test-key-for-ci")
AUTH_HEADERS = {"X-API-Key": SCAFFOLD_API_KEY}

from app.main import app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest_asyncio.fixture
async def client():
    """Async HTTP client wired to the FastAPI ASGI app."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers=AUTH_HEADERS,
        timeout=30.0,
    ) as c:
        yield c


# ---------------------------------------------------------------------------
# 1. Health endpoint
# ---------------------------------------------------------------------------

@pytest.mark.validate
@pytest.mark.timeout(10)
@pytest.mark.asyncio
async def test_health_endpoint(client):
    """GET / returns 200 and a parseable JSON body."""
    resp = await client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, dict)


# ---------------------------------------------------------------------------
# 2. RAG query round-trip
# ---------------------------------------------------------------------------

@pytest.mark.validate
@pytest.mark.timeout(180)
@pytest.mark.asyncio
async def test_rag_query_round_trip():
    """query_rag returns results with expected fields for a known domain.

    Timeout 180s (was 60s): cold-start CrossEncoder load + first batch on
    CPU-only inference can spend ~80s on the reranker pass. Warm runs
    finish in seconds. The 3x headroom keeps cold-start green without
    masking real perf regressions.

    Skipped when Milvus is empty (audit B3, post-§17.63 SSD migration).
    Pre-fix this test hard-failed on the ``assert len(docs) > 0`` below
    even though the failure mode was "no data to retrieve," not "retrieval
    pipeline broken."
    """
    from tests._milvus_helpers import skip_if_milvus_empty
    skip_if_milvus_empty()

    from app.modules.rag_pipeline import query_rag

    result = await query_rag("HNSW vector search", domain="eng", top_k=3)

    assert "results" in result
    docs = result["results"]
    assert len(docs) > 0, "Expected at least one result for spec-domain query"

    first = docs[0]
    # Every result must carry scoring info and a topic
    assert "scores" in first
    assert "title" in first
    # Scores dict should have the three scoring layers
    scores = first["scores"]
    assert "vector" in scores
    assert "rrf" in scores
    assert "rerank" in scores


# ---------------------------------------------------------------------------
# 3. Reranker direct call
# ---------------------------------------------------------------------------

@pytest.mark.validate
@pytest.mark.timeout(60)
@pytest.mark.asyncio
async def test_reranker_direct():
    """rerank() returns scored items with a backend indicator."""
    from app.rerankers import rerank

    query = "What is the TOON file format?"
    docs = [
        "TOON is a pipe-delimited knowledge format used by smokieRAGs.",
        "CI/CD pipelines automate software deployment.",
    ]

    result = rerank(query, docs)

    assert hasattr(result, "items"), "rerank result missing .items"
    assert len(result.items) == 2
    assert hasattr(result, "backend"), "rerank result missing .backend"
    assert result.backend in ("CrossEncoder", "cross_encoder", "rrf_fallback", "RRF_fallback")

    for item in result.items:
        assert hasattr(item, "score")
        assert hasattr(item, "index")
        assert isinstance(item.score, (int, float))


# ---------------------------------------------------------------------------
# 4. Job submission
# ---------------------------------------------------------------------------

@pytest.mark.validate
@pytest.mark.timeout(180)
@pytest.mark.asyncio
async def test_job_submission():
    """POST /ideas creates a job and returns job_id + status 'awaiting_confirmation'."""
    payload = {
        "idea": "List three sorting algorithms",
        "domain": "eng",
    }
    async with httpx.AsyncClient(
        base_url="http://localhost:8000", timeout=120.0,
        headers=AUTH_HEADERS
    ) as live:
        resp = await live.post("/ideas", json=payload)
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

    body = resp.json()
    assert "job_id" in body, f"Response missing job_id: {body}"
    assert body.get("status") == "awaiting_confirmation", f"Expected status 'awaiting_confirmation', got {body.get('status')}"
    assert isinstance(body["job_id"], str)
    assert len(body["job_id"]) > 0
