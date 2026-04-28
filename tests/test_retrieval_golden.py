"""
Golden retrieval regression tests.

Quick smoke check that known queries retrieve the expected documents
from the live RAG pipeline. Catches regressions after TOON edits,
embedding model changes, or Milvus re-indexing.

Run:  docker exec scaffold-orchestrator pytest tests/test_retrieval_golden.py -v
Tier: make validate
"""


import pytest

from app.modules.rag_pipeline import query_rag
# Per-query KB-availability skips below (3 queries currently active; 4 skipped
# pending KB content). The previous module-level pytestmark.skip was removed
# 2026-04-28 after live KB inspection (664 entries: eng=261, llm=218, rag=175,
# spec=8, prompt=0).

# ---------------------------------------------------------------------------
# Golden queries: (query, domain, expected_topic_substring)
#
# Each entry asserts that a document whose topic *contains* the substring
# appears in the top-3 results.  Substring matching keeps the fixture
# resilient to minor topic rewording in TOON files.
# ---------------------------------------------------------------------------

_NEEDS_PROMPT_KB = pytest.mark.skip(
    reason="prompt partition is empty (0 entries) - skip until prompt-domain TOONs are ingested"
)
_NEEDS_LLM_QUANTIZ = pytest.mark.skip(
    reason="llm partition (218 entries) does not currently include a quantization doc - skip until ingested"
)
_NEEDS_SPEC_TOON = pytest.mark.skip(
    reason="spec partition (8 entries) does not currently include the TOON spec doc - skip until ingested"
)

GOLDEN_QUERIES = [
    # --- prompt domain (currently 0 entries; skipped) ---
    pytest.param(
        "How does function calling work in LLM tool use?",
        "prompt", "function-calling",
        marks=_NEEDS_PROMPT_KB,
    ),
    pytest.param(
        "What is chain of thought prompting?",
        "prompt", "chain-of-thought",
        marks=_NEEDS_PROMPT_KB,
    ),
    # --- rag domain (175 entries) ---
    pytest.param(
        "How does hybrid search combine dense and sparse retrieval?",
        "rag", "hybrid",
    ),
    # --- llm domain (218 entries) ---
    pytest.param(
        "What is quantization and how does it reduce model size?",
        "llm", "quantiz",
        marks=_NEEDS_LLM_QUANTIZ,
    ),
    # --- spec domain (8 entries) ---
    pytest.param(
        "Describe the TOON file format specification and its pipeline stages",
        "spec", "toon",
        marks=_NEEDS_SPEC_TOON,
    ),
    # --- eng domain (261 entries) ---
    pytest.param(
        "What are common software design patterns like singleton or factory?",
        "eng", "pattern",
    ),
    pytest.param(
        "Explain the principles of test-driven development",
        "eng", "test",
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
)
async def test_golden_retrieval(query: str, domain: str, expected_substr: str):
    """Assert the expected document appears in top-3 for a golden query."""
    result = await query_rag(query, domain=domain, top_k=3)

    topics = [r["title"] for r in result["results"]]
    assert len(topics) > 0, f"No results returned for query: {query!r}"

    matched = any(expected_substr.lower() in t.lower() for t in topics)
    assert matched, (
        f"Expected title containing {expected_substr!r} in top-3, "
        f"got: {topics}"
    )
