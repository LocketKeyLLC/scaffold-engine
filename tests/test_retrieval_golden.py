"""
Golden retrieval regression tests.

Quick smoke check that known queries retrieve the expected documents
from the live RAG pipeline. Catches regressions after TOON edits,
embedding model changes, or Milvus re-indexing.

Run:  docker exec scaffold-orchestrator pytest tests/test_retrieval_golden.py -v
Tier: make validate

Assertion shape — title-substring, not entry-id. Each parametrization
asserts a case-insensitive substring of an expected topic appears in a
top-3 title. This is intentional: §17.211's corpus archaeology found
that topic-mode entry titles come from the LLM's RECORD_ENTRIES tool
call (§7.x extraction) and are non-deterministic across re-ingestion —
the same source URL re-ingested can produce different slugs. Substring
matching survives that drift; the goldens here would not survive an
exact entry-id assertion shape. (``scripts/score_retrieval.py`` +
``tests/fixtures/golden_set.json`` use the entry-id shape and pay for
that brittleness with the 0/20 coverage observation in §17.211.)

Corpus history. The §17.63 SSD migration left the Milvus collection
empty; §17.165 + §17.210 + §17.350 progressively restored content.
All 7 parametrizations are now active as of §17.350 (the three
previously-skipped queries — function-calling, hybrid-search, TOON
spec — were unblocked by ``scripts/seed_corpus_remainder.py`` which
ingests 3 hand-curated entries because no external Wikipedia/vendor
source for those exact titles exists). See ``OVERVIEW.md``
§§17.86, 17.92, 17.158, 17.165, 17.210, 17.211, 17.350 for the full
corpus rebuild arc.
"""


import pytest

from app.modules.rag_pipeline import query_rag
from tests._milvus_helpers import skip_if_milvus_empty
# Per-query KB-availability skips below (3 queries currently active; 4 skipped
# pending KB content). The previous module-level pytestmark.skip was removed
# 2026-04-28 after live KB inspection (664 entries: eng=261, llm=218, rag=175,
# spec=8, prompt=0). Audit B3 (2026-05-09) added the collection-level guard
# below so a fully-empty Milvus (e.g. post-§17.63 SSD migration) skips all
# parametrizations instead of hard-failing on `assert len(topics) > 0`.

# ---------------------------------------------------------------------------
# Golden queries: (query, domain, expected_topic_substring)
#
# Each entry asserts that a document whose topic *contains* the substring
# appears in the top-3 results.  Substring matching keeps the fixture
# resilient to minor topic rewording in TOON files.
# ---------------------------------------------------------------------------

# §17.92 skip-mark refresh + §17.350 unskip — the three remaining
# KB-content-dependent skips were closed by scripts/seed_corpus_remainder.py
# (3 hand-curated entries: function-calling/prompt, hybrid retrieval/rag,
# TOON v2 spec/spec). All 7 parametrizations now active.

GOLDEN_QUERIES = [
    # --- prompt domain ---
    # §17.92 ingested Chain-of-thought_prompting → prompt-engineering title
    # (second query). §17.350 added hand-curated function-calling entry
    # (first query, was _NEEDS_FUNCTION_CALLING_DOC).
    pytest.param(
        "How does function calling work in LLM tool use?",
        "prompt", "function-calling",
    ),
    pytest.param(
        "What is chain of thought prompting?",
        "prompt", "prompt engineering",
    ),
    # --- rag domain (Vector_database + Retrieval-augmented_generation seeded;
    # §17.350 added hand-curated hybrid-retrieval doc — was _NEEDS_HYBRID_SEARCH_DOC) ---
    pytest.param(
        "How does hybrid search combine dense and sparse retrieval?",
        "rag", "hybrid",
    ),
    # --- llm domain ---
    # §17.92 ingested Quantization_(signal_processing) (title 'Quantization
    # (signal processing) - Wikipedia'); 10 entries landed. Substring
    # 'quantiz' is case-insensitive so it matches 'Quantization'.
    pytest.param(
        "What is quantization and how does it reduce model size?",
        "llm", "quantiz",
    ),
    # --- spec domain (§17.350 added TOON v2 spec — was _NEEDS_SPEC_TOON) ---
    pytest.param(
        "Describe the TOON file format specification and its pipeline stages",
        "spec", "toon",
    ),
    # --- eng domain ---
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
# Timeout 300s (was 60s): post-§17.63 repopulation pushed KB size up
# enough that the CrossEncoder reranker on CPU takes ~60-200s per
# query (verified via direct `make test`). 60s tripped on every active
# query; 300s gives 1.5× headroom over the slowest observed run while
# still flagging genuine perf regressions.
@pytest.mark.timeout(300)
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, domain, expected_substr",
    GOLDEN_QUERIES,
)
async def test_golden_retrieval(query: str, domain: str, expected_substr: str):
    """Assert the expected document appears in top-3 for a golden query."""
    skip_if_milvus_empty()
    result = await query_rag(query, domain=domain, top_k=3)

    topics = [r["title"] for r in result["results"]]
    assert len(topics) > 0, f"No results returned for query: {query!r}"

    matched = any(expected_substr.lower() in t.lower() for t in topics)
    assert matched, (
        f"Expected title containing {expected_substr!r} in top-3, "
        f"got: {topics}"
    )
