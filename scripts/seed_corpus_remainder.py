"""§17.350 / §17.595 — Seed the KB-content-dependent golden docs so
``test_golden_retrieval`` is restorable offline via one idempotent script.

§17.350 seeded the three hand-curated gaps (function-calling/prompt,
hybrid/rag, TOON-spec/spec). §17.595 added the two docs that had been
Wikipedia-sourced (chain-of-thought/prompt, quantization/llm) as curated
stand-ins, because the live corpus loses them on the compose-down pattern
and live re-ingest is fragile + slug-nondeterministic. Running this after a
corpus wipe restores all five non-``eng`` golden cases (``eng`` retrieval
comes from the bulk github/wiki corpus, not this script).

The three original hand-curated gaps are named in the historical skip
rationale in ``tests/test_retrieval_golden.py``:

  * ``_NEEDS_FUNCTION_CALLING_DOC`` — prompt partition needs a title
    containing 'function-calling'. Wikipedia has no such article (the
    topic is a sub-section of Prompt_engineering whose title is just
    "Prompt engineering"). Hand-curated.
  * ``_NEEDS_HYBRID_SEARCH_DOC`` — rag partition needs a title containing
    'hybrid'. Wikipedia has no Hybrid_search/Hybrid_retrieval article;
    Okapi_BM25 + Learning_to_rank don't carry 'hybrid' in their titles.
    Hand-curated.
  * ``_NEEDS_SPEC_TOON`` — spec partition needs a TOON spec doc. TOON
    (Token-Oriented Object Notation) is project-internal; no external
    Wikipedia/vendor source exists. Hand-curated from the actual
    in-repo TOON v2 schema in app/utils/milvus_utils.py + gt_browser.py.

Same mechanic as scripts/seed_eng_topologies.py (§17.149) and
scripts/seed_eng_digital.py (§17.154): build a list of curated entries,
call ``rag_pipeline.ingest_entries`` with the right ``domain`` per entry.

Run from inside the orchestrator container:

    docker exec scaffold-orchestrator python scripts/seed_corpus_remainder.py
    docker exec scaffold-orchestrator python scripts/seed_corpus_remainder.py --dry-run

Idempotent — §9.x content-hash dedup rejects exact re-runs as no-ops.

Exit codes:
  0 success
  1 bad CLI flags
  2 ingest path returned an error
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Any

logger = logging.getLogger("scaffold.seed_corpus_remainder")

SOURCE_TYPE = "curated"
CONFIDENCE = 0.90  # hand-curated by an engineer

# Each tuple: (partition, entry dict). The seed groups entries by
# partition because ``ingest_entries`` takes a single domain= per call.
SEEDS: list[tuple[str, dict[str, Any]]] = [
    (
        "prompt",
        {
            "title": "LLM function-calling (tool use) request/response loop",
            "tags": ["function-calling", "tool_use", "prompt", "llm", "anthropic", "openai"],
            "source_url": "internal:scaffold-engine/docs/function-calling.md",
            "content": (
                "Function-calling (also called tool use) is the protocol by "
                "which a chat-model client lets the LLM invoke developer-defined "
                "functions during a conversation. The pattern: the client sends "
                "a tools[] array along with messages, each tool carrying a name, "
                "description, and JSON-Schema input contract. When the model "
                "decides a tool is needed it emits a tool_use block (Anthropic) "
                "or tool_calls entry (OpenAI) containing the tool name and "
                "structured arguments matching the schema. The client executes "
                "the function, then sends a follow-up message with a tool_result "
                "block carrying the output (keyed by tool_use_id). The model "
                "continues from there — same conversation, full prior context "
                "preserved. Multi-turn loops continue until the model emits "
                "stop_reason='end_turn' (no further tool calls). Tool choice can "
                "be steered: 'auto' lets the model decide, 'any' forces some tool, "
                "'tool' forces a named tool, 'none' suppresses tools. Strict "
                "mode (strict=true) enforces the input schema at the API layer, "
                "rejecting calls with missing/wrong-shape arguments. The pattern "
                "underlies most agentic LLM workflows including the scaffold-engine "
                "research_agent (uses RECORD_ENTRIES tool to capture findings) "
                "and DAG executor (CodeGen + verifier tool roundtrips)."
            ),
        },
    ),
    (
        "rag",
        {
            "title": "Hybrid retrieval: dense + sparse fusion (BM25 + vectors + RRF)",
            "tags": ["hybrid", "retrieval", "rag", "rrf", "bm25", "milvus", "dense", "sparse"],
            "source_url": "internal:scaffold-engine/docs/hybrid-retrieval.md",
            "content": (
                "Hybrid retrieval combines dense semantic vectors with sparse "
                "keyword scoring to get the best of both: dense vectors capture "
                "synonyms and conceptual proximity but miss rare/exact terms; "
                "sparse methods (BM25, TF-IDF, SPLADE) match literal terms but "
                "miss paraphrase. The standard pipeline: run the same query "
                "through (1) a dense embedder (e.g. nomic-embed-text, qwen3-"
                "embedding) producing a vector, search via cosine/IP against "
                "the vector index; (2) a sparse encoder (BM25 over a tokenized "
                "inverted index) producing a per-doc score. Fuse the two ranked "
                "lists with Reciprocal Rank Fusion (RRF): for each document, "
                "fused_score = sum(1 / (k + rank_in_list)) across all lists, "
                "with k≈60 as the standard smoothing constant. Top-N from the "
                "fused list is then optionally reranked by a cross-encoder "
                "(query+doc pair scored jointly, much higher quality at higher "
                "latency cost). Milvus 2.4+ supports hybrid search natively via "
                "the hybrid_search API; before that, the pattern was client-"
                "side fusion. scaffold-engine's rag_pipeline runs dense-only "
                "retrieval + cross-encoder rerank; sparse fusion is on the "
                "roadmap for queries with rare-term gaps. Hybrid retrieval "
                "consistently beats pure-dense or pure-sparse on benchmarks "
                "with mixed query shapes (BEIR, MS MARCO subsets)."
            ),
        },
    ),
    (
        "spec",
        {
            "title": "TOON v2 (Token-Oriented Object Notation) entry format specification",
            "tags": ["toon", "spec", "format", "milvus", "ground_truth", "internal"],
            "source_url": "internal:scaffold-engine/docs/toon-v2-spec.md",
            "content": (
                "TOON (Token-Oriented Object Notation) v2 is the row format used "
                "by the scaffold-engine Milvus collection (named toon_v2). Each "
                "row represents one Ground Truth knowledge entry retrievable via "
                "the RAG pipeline. The schema lives in app/utils/milvus_utils.py "
                "and is consumed by app/modules/gt_browser.py for listing, "
                "search, and content fetch. Required fields: entry_id (UUID4, "
                "primary key); canonical_text (TEXT, the embedding source); "
                "embedding (FLOAT_VECTOR, 512-dim, MRL-truncated from the "
                "embedder's native dim); domain (VARCHAR, partition key — one of "
                "eng/eng_design/llm/rag/spec/prompt/code/qa); source_type "
                "(VARCHAR — wiki_article, github_code, github_release, curated, "
                "disputed_claim, …); source_url (VARCHAR, provenance); "
                "confidence (FLOAT, 0.0-1.0); content_hash (VARCHAR, SHA256 of "
                "canonical_text — used for dedup at ingest time); created_at "
                "(BIGINT, unix ms); superseded_by (VARCHAR, nullable — points "
                "at a newer entry's entry_id when a version chain exists); "
                "version_chain_root (VARCHAR, the original entry_id at the "
                "head of a version chain). The 3-tier ingest dedup uses "
                "cosine similarity against existing entries: > 0.95 rejects "
                "(exact-ish duplicate), 0.90-0.95 creates a version chain "
                "(supersedes the older), < 0.90 inserts as a new entry. "
                "Retrieval filters superseded entries by default; "
                "include_history=true opts into the full chain. Partition keys "
                "enable per-domain partition-pruning at query time for "
                "10-100× speedup on focused queries."
            ),
        },
    ),
    # §17.595 — the prompt/chain-of-thought and llm/quantization golden docs
    # originally came from live Wikipedia ingests (§17.92:
    # Chain-of-thought_prompting, Quantization_(signal_processing)). Those
    # were lost with the corpus (compose-down pattern) and live re-ingest is
    # fragile + produces non-deterministic slugs. Curated stand-ins here make
    # `test_golden_retrieval` restorable offline via this one script. Titles
    # carry the substrings the goldens assert ('prompt engineering', 'quantiz').
    (
        "prompt",
        {
            "title": "Chain-of-thought prompting — a prompt engineering technique",
            "tags": ["chain-of-thought", "cot", "prompt engineering", "prompt", "reasoning", "llm"],
            "source_url": "internal:scaffold-engine/docs/chain-of-thought.md",
            "content": (
                "Chain-of-thought (CoT) prompting is a prompt engineering "
                "technique that elicits intermediate reasoning steps from a large "
                "language model before it commits to a final answer. Instead of "
                "asking for the answer directly, the prompt instructs the model to "
                "'think step by step' (zero-shot CoT) or supplies worked examples "
                "that each show the reasoning trace (few-shot CoT). Making the "
                "intermediate steps explicit measurably improves accuracy on "
                "multi-step arithmetic, commonsense, and symbolic-reasoning tasks, "
                "because the model allocates more forward-pass computation to the "
                "sub-problems and conditions each step on the previously generated "
                "ones. Variants extend the idea: self-consistency samples several "
                "independent chains and majority-votes the final answers; "
                "least-to-most decomposes a hard problem into an ordered sequence "
                "of easier sub-questions; tree-of-thought explores and backtracks "
                "over a branching space of partial reasoning. CoT is most effective "
                "on sufficiently large models (an emergent capability) and pairs "
                "well with tool use — the model reasons about which tool to call, "
                "then reads the result back into the chain. Downsides: longer "
                "outputs cost more tokens and latency, and a fluent-looking chain "
                "can still reach a wrong answer (reasoning is not a correctness "
                "guarantee). In scaffold-engine, CoT-style prompts back the "
                "research decompose/gap-analysis stages and the node verifier's "
                "rationale before its pass/fail verdict."
            ),
        },
    ),
    (
        "llm",
        {
            "title": "Quantization for LLMs: shrinking model size with int8/int4",
            "tags": ["quantization", "quantize", "int8", "int4", "gguf", "llm", "inference"],
            "source_url": "internal:scaffold-engine/docs/quantization.md",
            "content": (
                "Quantization reduces the memory footprint and inference cost of a "
                "large language model by storing its weights (and sometimes "
                "activations) in a lower-precision numeric format than the 16- or "
                "32-bit floats used for training. Mapping fp16 weights to 8-bit "
                "integers roughly halves model size; 4-bit halves it again, so a "
                "7-billion-parameter model that needs ~14 GB at fp16 fits in "
                "~4 GB at int4 — the difference between needing a datacenter GPU "
                "and running on a laptop or CPU. The mapping stores a scale (and "
                "optionally a zero-point) per group of weights so the original "
                "range is approximately recoverable at compute time. Post-training "
                "quantization (PTQ) converts an already-trained model directly — "
                "GPTQ, AWQ, and llama.cpp's GGUF k-quants are common PTQ schemes; "
                "quantization-aware training (QAT) instead simulates the rounding "
                "during fine-tuning so the model adapts to the precision loss. The "
                "trade-off is a small, usually acceptable drop in output quality "
                "(perplexity rises slightly) in exchange for large gains in speed "
                "and reduced VRAM/RAM. scaffold-engine runs CPU-only inference via "
                "Ollama, which serves quantized GGUF builds (e.g. q4_K_M) so 7B "
                "models stay within host memory — the same rationale that keeps "
                "the embedder and reranker CPU-tractable."
            ),
        },
    ),
]


def _build_entry(seed: dict[str, Any]) -> dict[str, Any]:
    """Match ingest_entries() expected shape (mirrors seed_eng_topologies)."""
    return {
        "title": seed["title"],
        "content": seed["content"].strip(),
        "domain_tags": list(seed["tags"]),
        "source_url": seed["source_url"],
        "source_type": SOURCE_TYPE,
        "confidence": CONFIDENCE,
    }


async def _with_http_clients(coro):
    from app.utils import http_clients
    http_clients.init_clients()
    try:
        return await coro
    finally:
        await http_clients.close_clients()


async def ingest_all() -> dict[str, dict]:
    """Group seeds by partition, ingest each batch with the right domain.

    ``ingest_entries`` takes a single ``domain=`` so we make one call per
    partition. Returns ``{partition: stats_dict}`` for the operator to read.
    """
    from app.modules.rag_pipeline import ingest_entries

    async def _run() -> dict[str, dict]:
        by_partition: dict[str, list[dict]] = {}
        for partition, seed in SEEDS:
            by_partition.setdefault(partition, []).append(_build_entry(seed))
        out: dict[str, dict] = {}
        for partition, entries in by_partition.items():
            logger.info(
                "ingest_partition_start: partition=%s n=%d",
                partition, len(entries),
            )
            stats = await ingest_entries(entries, domain=partition)
            out[partition] = stats
            logger.info(
                "ingest_partition_done: partition=%s stats=%s", partition, stats,
            )
        return out

    return await _with_http_clients(_run())


def _print_plan() -> None:
    print(f"DRY RUN — would ingest {len(SEEDS)} curated entries:")
    for partition, seed in SEEDS:
        e = _build_entry(seed)
        print(
            f"  - partition={partition!r:10s} title={e['title']!r:80s} "
            f"({len(e['content'])} chars, source={e['source_url']})"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0] if __doc__ else None,
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the ingest plan, don't write.",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true",
        help="Enable INFO logging.",
    )
    args = parser.parse_args(argv)
    if args.verbose:
        logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    if args.dry_run:
        _print_plan()
        return 0

    try:
        stats = asyncio.run(ingest_all())
    except Exception as exc:
        logger.error("ingest_failed: %s", exc, exc_info=True)
        print(f"ingest failed: {exc}", file=sys.stderr)
        return 2

    print("=== seed_corpus_remainder summary ===")
    for partition, s in stats.items():
        print(f"  partition={partition}: {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
