"""
Golden retrieval regression tests.

Quick smoke check that known queries retrieve the expected documents
from the live RAG pipeline. Catches regressions after TOON edits,
embedding model changes, or Milvus re-indexing.

Run:  docker exec scaffold-orchestrator pytest tests/test_retrieval_golden.py -v
Tier: make validate
"""

import sys
sys.path.insert(0, "/app")

import pytest

from app.modules.rag_pipeline import query_rag


# ---------------------------------------------------------------------------
# Golden queries: (query, domain, expected_topic_substring)
#
# Each entry asserts that a document whose topic *contains* the substring
# appears in the top-3 results.  Substring matching keeps the fixture
# resilient to minor topic rewording in TOON files.
# ---------------------------------------------------------------------------

GOLDEN_QUERIES = [
    # --- prompt domain (30 entries) ---
    (
        "How does function calling work in LLM tool use?",
        "prompt",
        "function-calling",
    ),
    (
        "What is chain of thought prompting?",
        "prompt",
        "chain-of-thought",
    ),
    # --- rag domain (15 entries) ---
    (
        "How does hybrid search combine dense and sparse retrieval?",
        "rag",
        "hybrid",
    ),
    # --- llm domain (13 entries) ---
    (
        "What is quantization and how does it reduce model size?",
        "llm",
        "quantiz",
    ),
    # --- spec domain (11 entries) ---
    (
        "Describe the TOON file format specification and its pipeline stages",
        "spec",
        "toon",
    ),
    # --- eng domain (14 entries) ---
    (
        "What are common software design patterns like singleton or factory?",
        "eng",
        "pattern",
    ),
    (
        "Explain the principles of test-driven development",
        "eng",
        "test",
    ),
]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.validate
@pytest.mark.timeout(60)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, domain, expected_substr",
    GOLDEN_QUERIES,
    ids=[f"{q[1]}-{q[2]}" for q in GOLDEN_QUERIES],
)
async def test_golden_retrieval(query: str, domain: str, expected_substr: str):
    """Assert the expected document appears in top-3 for a golden query."""
    result = await query_rag(query, domain=domain, top_k=3)

    topics = [r["topic"] for r in result["results"]]
    assert len(topics) > 0, f"No results returned for query: {query!r}"

    matched = any(expected_substr.lower() in t.lower() for t in topics)
    assert matched, (
        f"Expected topic containing {expected_substr!r} in top-3, "
        f"got: {topics}"
    )
