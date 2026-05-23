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

Why three queries skip. The corpus regression documented in §17.158
(Milvus collection rebuilt empty post-§17.63 SSD migration; only
``eng`` re-landed; ``llm`` / ``rag`` / ``prompt`` partitions partial)
plus the §17.165 + §17.210 repopulation passes left the knowledge base
without the specific Wikipedia / vendor documents the three skipped
parametrizations need (function-calling, hybrid-search, TOON spec).
Each skip's ``reason=`` names the exact missing source so a future
ingest pass can flip it back on. §17.92 already flipped 2 of the
original 5 §17.86 skips to active; the remaining 3 are corpus-content
dependencies the goldens cannot satisfy until the source-of-truth
documents are ingested. See ``OVERVIEW.md`` §§17.86, 17.92, 17.158,
17.165, 17.210, 17.211 for the full corpus history.
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

# §17.92 skip-mark refresh — three named blockers replace the prior generic
# "partition is empty" rationale. Each names the specific content that
# would unblock its parametrization, and the §17.92 ingest pass landed
# Chain-of-thought_prompting (prompt) + Quantization_(signal_processing)
# (llm) which flipped two previously-skipped queries to active.
_NEEDS_FUNCTION_CALLING_DOC = pytest.mark.skip(
    reason="KB-content dependency (corpus regression §17.158, partial recovery "
    "§17.165 + §17.210): prompt partition lacks a doc whose title contains "
    "'function-calling'. Wikipedia has no Function_calling article (the page "
    "404s; the topic is covered as a sub-section of Prompt_engineering, "
    "whose <title> is 'Prompt engineering - Wikipedia'). Skip until a "
    "vendor-doc or hand-curated source named for function-calling "
    "specifically is ingested. See OVERVIEW.md §17.158 for the post-§17.63 "
    "Milvus rebuild that emptied this partition."
)
_NEEDS_HYBRID_SEARCH_DOC = pytest.mark.skip(
    reason="KB-content dependency (corpus regression §17.158, partial recovery "
    "§17.165 + §17.210): rag partition lacks a doc whose title contains "
    "'hybrid'. Wikipedia has no Hybrid_search / Hybrid_retrieval article "
    "(both 404). The available related Wikipedia articles (Okapi_BM25, "
    "Learning_to_rank, Semantic_search) don't carry 'hybrid' in their "
    "titles. Skip until a vendor blog post or paper-derived doc with "
    "'hybrid' in title is ingested. See OVERVIEW.md §17.158 for the "
    "post-§17.63 Milvus rebuild that left this partition under-seeded."
)
_NEEDS_SPEC_TOON = pytest.mark.skip(
    reason="KB-content dependency (corpus regression §17.158): spec partition "
    "lacks a TOON spec doc — TOON (Token-Oriented Object Notation) is "
    "project-internal with no external Wikipedia or vendor source. "
    "docs/toon/toon_validator_reference/ exists but is a Python reference "
    "implementation, not a spec document. Skip until a markdown spec is "
    "written and ingested as a custom URL or file upload. See OVERVIEW.md "
    "§17.158 for the broader corpus-regression context — this skip pre-dates "
    "that incident but shares the resolution shape (ingest source-of-truth, "
    "then drop the skip mark)."
)

GOLDEN_QUERIES = [
    # --- prompt domain ---
    # §17.92 ingested Chain-of-thought_prompting which serves a Wikipedia
    # page whose <title> is 'Prompt engineering - Wikipedia' (the CoT URL
    # has no redirect but Wikipedia renders the parent prompt-engineering
    # article body with that title). 10 entries landed; the second query
    # below is now active against the substring 'prompt engineering'.
    pytest.param(
        "How does function calling work in LLM tool use?",
        "prompt", "function-calling",
        marks=_NEEDS_FUNCTION_CALLING_DOC,
    ),
    pytest.param(
        "What is chain of thought prompting?",
        "prompt", "prompt engineering",
    ),
    # --- rag domain (Vector_database + Retrieval-augmented_generation seeded;
    # no hybrid-titled doc yet — see _NEEDS_HYBRID_SEARCH_DOC for the block) ---
    pytest.param(
        "How does hybrid search combine dense and sparse retrieval?",
        "rag", "hybrid",
        marks=_NEEDS_HYBRID_SEARCH_DOC,
    ),
    # --- llm domain ---
    # §17.92 ingested Quantization_(signal_processing) (title 'Quantization
    # (signal processing) - Wikipedia'); 10 entries landed. Substring
    # 'quantiz' is case-insensitive so it matches 'Quantization'.
    pytest.param(
        "What is quantization and how does it reduce model size?",
        "llm", "quantiz",
    ),
    # --- spec domain (no TOON spec doc yet — see _NEEDS_SPEC_TOON) ---
    pytest.param(
        "Describe the TOON file format specification and its pipeline stages",
        "spec", "toon",
        marks=_NEEDS_SPEC_TOON,
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
