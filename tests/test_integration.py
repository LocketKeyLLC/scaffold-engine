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

# §17.1008 — read the key at CALL time, not at import time.
#
# `_live_write_guard.install()` deliberately blanks SCAFFOLD_API_KEY in the test
# process so a unit test that escapes its mocks cannot authenticate as the
# operator (§17.934), and `uninstall()` restores it for `integration`-marked
# tests. A module-level snapshot is therefore a value captured at collection and
# used minutes later, across an env the suite intentionally mutates — fragile
# even where it happens to work.
#
# §17.1009 — this WAS the fix. It shipped without that claim, because I had
# reasoned that pytest collects every module before running any test, so the
# snapshot could only ever hold the real key. The first full suite after the
# change went 6,093 passed / 0 failed, and the mechanism I argued myself out of
# is the one that fits: in a full run this module is imported late enough —
# after tests/integration/ has run and after non-integration tests installed the
# guard — that the snapshot captured the BLANKED value. Every subset that passed
# did so because too few non-integration tests preceded it.
def _auth_headers() -> dict:
    return {"X-API-Key": os.environ.get("SCAFFOLD_API_KEY", "test-key-for-ci")}


class _AuthHeaders(dict):
    """Behaves like the dict it replaced, but resolves at use rather than at
    import — every existing `headers=AUTH_HEADERS` call site keeps working."""

    def __init__(self):
        super().__init__()

    def __iter__(self):
        return iter(_auth_headers())

    def keys(self):
        return _auth_headers().keys()

    def items(self):
        return _auth_headers().items()

    def __getitem__(self, k):
        return _auth_headers()[k]

    def __len__(self):
        return len(_auth_headers())


SCAFFOLD_API_KEY = os.environ.get("SCAFFOLD_API_KEY", "test-key-for-ci")
AUTH_HEADERS = _AuthHeaders()


def _auth_diagnosis() -> str:
    """Describe the key WITHOUT printing it, for a 401's error message."""
    raw = os.environ.get("SCAFFOLD_API_KEY")
    if raw is None:
        return "SCAFFOLD_API_KEY is UNSET in this process"
    if raw == "":
        return (
            "SCAFFOLD_API_KEY is EMPTY — _live_write_guard.install() blanks it "
            "and uninstall() should have restored it for this integration-marked test"
        )
    return f"SCAFFOLD_API_KEY is set (length {len(raw)})"

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

# §17.1008 — two problems with this test, both found by it failing ~1 run in 2
# while passing in isolation:
#
# 1. THE TIMEOUT WAS SHORTER THAN THE WORK. `/ideas` runs Phase 1 SYNCHRONOUSLY
#    — refinement plus a feasibility pass, two LLM calls — and the console's own
#    copy tells operators to expect 1-9 minutes for it. The client was capped at
#    120s inside a 180s pytest budget. Alone on an idle box a trivial idea
#    squeaks under; inside the full suite, competing for the same CPU
#    inference, it does not. Nothing was wrong with the engine: the assertion
#    was just being made before the work could finish. Both budgets now match
#    the documented window (`timeout(900)` is the convention for live tests
#    here).
#
# 2. IT LEFT ITS JOB BEHIND. Every full-suite run created a real job and never
#    cleaned it up — 14 of them had accumulated in this database, each having
#    burned a real Phase 1. A live test may spend inference; it should not
#    quietly grow the operator's job list forever. It now cancels what it
#    created, in a finally so a failed assertion still cleans up.
@pytest.mark.validate
@pytest.mark.timeout(900)
@pytest.mark.asyncio
async def test_job_submission():
    """POST /ideas creates a job and returns job_id + status 'awaiting_confirmation'."""
    payload = {
        "idea": "List three sorting algorithms",
        "domain": "eng",
    }
    job_id = None
    async with httpx.AsyncClient(
        base_url="http://localhost:8000", timeout=600.0,
        headers=AUTH_HEADERS
    ) as live:
        try:
            resp = await live.post("/ideas", json=payload)
            assert resp.status_code == 200, (
                f"Expected 200, got {resp.status_code}: {resp.text}\n"
                # §17.1008 — this test fails in the FULL suite and passes both
                # in isolation and against every subset tried (all 189
                # test_*.py files before it; the whole tests/integration/ dir
                # before it). It has not been root caused. When it next fails,
                # this line says whether auth was the reason.
                f"auth diagnosis: {_auth_diagnosis()}"
            )

            body = resp.json()
            assert "job_id" in body, f"Response missing job_id: {body}"
            job_id = body["job_id"]
            assert body.get("status") == "awaiting_confirmation", f"Expected status 'awaiting_confirmation', got {body.get('status')}"
            assert isinstance(body["job_id"], str)
            assert len(body["job_id"]) > 0
        finally:
            # Clean up regardless of outcome. Cancel is idempotent and
            # non-destructive (the brief is preserved), so this is safe even if
            # the assertions above never ran.
            if job_id:
                try:
                    await live.post(f"/jobs/{job_id}/cancel", json={})
                except Exception:  # never let cleanup mask the real failure
                    pass
