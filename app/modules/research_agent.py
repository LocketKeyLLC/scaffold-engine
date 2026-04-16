"""Scaffold Engine — Autonomous research agent.

/research <topic> decomposes a topic into sub-queries, searches via SearXNG,
fetches and extracts content, distills facts via LLM, ingests into Milvus,
then runs gap analysis and iterates until coverage converges.

Architecture: planner-executor loop with fan-out search / fan-in extraction.
Two-tier model strategy: model_verifier (7b) for decomposition/extraction,
model_general (heavy) reserved for final synthesis only.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from typing import AsyncGenerator

from app import model_router
from app.config import settings, get_model
from app.modules.rag_pipeline import ingest_entries, _embed_query
from app.utils.llm_parsing import parse_json_object, parse_json_array

logger = logging.getLogger("scaffold.research")


# ---------------------------------------------------------------------------
# State container
# ---------------------------------------------------------------------------

@dataclass
class ResearchState:
    topic: str
    depth: str = "medium"
    domain: str = "eng"
    iteration: int = 0
    search_history: set = field(default_factory=set)
    url_history: set = field(default_factory=set)
    all_entries: list = field(default_factory=list)
    total_ingested: int = 0
    total_rejected: int = 0
    outline_facets: list = field(default_factory=list)
    covered_facets: set = field(default_factory=set)
    gap_queries: list = field(default_factory=list)

    @property
    def max_iterations(self) -> int:
        return {"shallow": 1, "medium": 2, "deep": 4}.get(self.depth, 2)


# ---------------------------------------------------------------------------
# SSE helper
# ---------------------------------------------------------------------------

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


# ---------------------------------------------------------------------------
# Step 1: Topic decomposition
# ---------------------------------------------------------------------------

DECOMPOSE_SYSTEM = """You are a research planner. Decompose the given topic into
keyword-based search engine queries (3-8 words each, NOT natural language questions).

Rules:
- Produce 3-8 distinct facets covering DIFFERENT aspects of the topic
- Each query targets DIFFERENT information (no overlap)
- Include the topic's core terms for relevance
- Mix overview queries with specific detail queries
- Simple topics: 3-4 queries. Medium: 5-6. Complex: 7-8.
- search_category must be one of: general, news, science, it

EXAMPLE 1 — Topic: "Redis caching strategies"
{
  "topic_complexity": "medium",
  "facets": ["eviction policies", "cache patterns", "persistence", "cluster scaling", "monitoring"],
  "queries": [
    {"query": "Redis eviction policy LRU LFU comparison", "facet": "eviction policies", "search_category": "it"},
    {"query": "Redis cache aside write through patterns", "facet": "cache patterns", "search_category": "it"},
    {"query": "Redis RDB AOF persistence tradeoffs", "facet": "persistence", "search_category": "it"},
    {"query": "Redis cluster sharding horizontal scaling", "facet": "cluster scaling", "search_category": "it"},
    {"query": "Redis monitoring latency metrics tools", "facet": "monitoring", "search_category": "it"}
  ]
}

EXAMPLE 2 — Topic: "WebAssembly serverless edge computing"
{
  "topic_complexity": "complex",
  "facets": ["wasm runtimes", "cold start performance", "edge platforms", "language support"],
  "queries": [
    {"query": "WebAssembly runtime wasmtime wasmer comparison", "facet": "wasm runtimes", "search_category": "it"},
    {"query": "WASM serverless cold start latency benchmarks", "facet": "cold start performance", "search_category": "it"},
    {"query": "Cloudflare Workers Fastly edge WASM deployment", "facet": "edge platforms", "search_category": "it"},
    {"query": "WebAssembly Rust Go language compile support", "facet": "language support", "search_category": "it"}
  ]
}

OUTPUT FORMAT (strict JSON, no markdown fences):
{
  "topic_complexity": "simple|medium|complex",
  "facets": ["facet1", "facet2", "facet3", "..."],
  "queries": [
    {"query": "keyword search terms", "facet": "which facet this covers", "priority": "high|medium|low", "search_category": "general"}
  ]
}"""


async def _decompose_topic(
    topic: str,
    model: str,
    existing_facets: list | None = None,
    gap_focus: str | None = None,
) -> dict:
    """Decompose topic into search queries. Returns parsed dict or fallback."""
    prompt = f"Decompose this research topic into search queries:\n\nTOPIC: {topic}"
    if existing_facets:
        prompt += f"\n\nAlready covered facets (do NOT repeat): {', '.join(existing_facets)}"
    if gap_focus:
        prompt += f"\n\nFocus specifically on these gaps: {gap_focus}"

    resp = await model_router.generate(
        prompt,
        model=model,
        system=DECOMPOSE_SYSTEM,
        temperature=0.4,
        max_tokens=2048,
    )

    if resp.success:
        parsed = parse_json_object(resp.text)
        if parsed and "queries" in parsed:
            facets = parsed.get("facets", [])
            if len(facets) >= 2:
                return parsed
            # Retry once — model produced too few facets
            logger.info("decomposition_retry: got %d facets, retrying with explicit instruction", len(facets))
            retry_prompt = (
                f"Decompose this research topic into search queries:\n\n"
                f"TOPIC: {topic}\n\n"
                f"IMPORTANT: Break into at least 3 distinct subtopics. "
                f"Your previous attempt only produced {len(facets)} facet(s). "
                f"Each facet must cover a DIFFERENT aspect of the topic."
            )
            retry_resp = await model_router.generate(
                retry_prompt,
                model=model,
                system=DECOMPOSE_SYSTEM,
                temperature=0.5,
                max_tokens=2048,
            )
            if retry_resp.success:
                retry_parsed = parse_json_object(retry_resp.text)
                if retry_parsed and "queries" in retry_parsed:
                    return retry_parsed
            # If retry also fails, fall through to fallback below

    # Fallback: generate basic queries
    return {
        "topic_complexity": "medium",
        "facets": [topic],
        "queries": [
            {"query": topic, "facet": topic, "priority": "high", "search_category": "general"},
            {"query": f"{topic} best practices", "facet": topic, "priority": "medium", "search_category": "general"},
            {"query": f"{topic} implementation guide", "facet": topic, "priority": "medium", "search_category": "it"},
            {"query": f"{topic} common issues", "facet": topic, "priority": "low", "search_category": "general"},
        ],
    }


# ---------------------------------------------------------------------------
# Step 2: SearXNG search + fetch
# ---------------------------------------------------------------------------

async def _search_queries(
    queries: list[dict],
    state: ResearchState,
) -> list[dict]:
    """Run SearXNG searches for each query. Returns list of result dicts."""
    from app.utils.http_clients import get_searxng_client

    all_results = []
    client = get_searxng_client()

    for q in queries[:settings.research_max_queries]:
        query_text = q["query"]
        if query_text in state.search_history:
            continue

        try:
            resp = await client.get(
                "/search",
                params={
                    "q": query_text,
                    "format": "json",
                    "categories": q.get("search_category", "general"),
                },
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])[:10]
                for r in results:
                    url = r.get("url", "")
                    if url and url not in state.url_history:
                        state.url_history.add(url)
                        all_results.append({
                            "title": r.get("title", ""),
                            "url": url,
                            "content": r.get("content", ""),
                            "facet": q.get("facet", ""),
                        })
            state.search_history.add(query_text)
        except Exception as e:
            logger.warning("research_search_failed: query='%s' error=%s", query_text, e)

        await asyncio.sleep(settings.research_searxng_delay)

    return all_results[:settings.research_max_urls_per_iteration]


# ---------------------------------------------------------------------------
# Step 3: LLM distillation
# ---------------------------------------------------------------------------

EXTRACT_SYSTEM = """You are a knowledge extraction engine. Given search results about a topic,
extract atomic, self-contained factual entries.

Rules:
- Each entry is ONE fact that can be understood without surrounding context
- Be specific: include numbers, names, versions, dates where applicable
- Assign confidence: 1.0 = verified fact, 0.7 = secondary source, 0.4 = opinion/speculation
- Discard noise, opinions, marketing language
- 5-15 entries per batch
- Content must NOT contain escaped quotes or backslashes

OUTPUT FORMAT (strict JSON array, no markdown fences):
[
  {
    "title": "Short descriptive title",
    "content": "Self-contained factual statement. Technically precise.",
    "tags": "comma,separated,tags",
    "source": "URL",
    "confidence_score": 0.85,
    "source_type": "tech_docs|news|community|official_docs|curated",
    "facet": "which facet of the topic this covers"
  }
]"""

EXTRACT_PROMPT = """Extract factual knowledge entries from these search results about: {topic}

Search results:
---
{results}
---

Return ONLY the JSON array."""


async def _extract_entries(
    results: list[dict],
    topic: str,
    model: str,
) -> list[dict]:
    """Distill search results into knowledge entries via LLM."""
    if not results:
        return []

    # Batch results to stay within context limits
    batch_size = 10
    all_entries = []

    for i in range(0, len(results), batch_size):
        batch = results[i:i + batch_size]
        entries = []
        results_text = "\n\n".join(
            f"Title: {r['title']}\nURL: {r['url']}\nSnippet: {r['content']}"
            for r in batch
        )

        resp = await model_router.generate(
            EXTRACT_PROMPT.format(topic=topic, results=results_text),
            model=model,
            system=EXTRACT_SYSTEM,
            temperature=0.1,
            max_tokens=4096,
        )

        if resp.success and resp.text and len(resp.text.strip()) > 5:
            entries = parse_json_array(resp.text) or []
            if entries:
                all_entries.extend(entries)
                logger.info("extraction_batch: %d entries from batch %d", len(entries), i // batch_size + 1)
            else:
                logger.warning("extraction_parse_failed: batch=%d raw_len=%d raw_preview=%s",
                               i // batch_size + 1, len(resp.text), resp.text[:300])
        else:
            logger.warning("extraction_llm_failed: batch=%d success=%s raw_len=%d error=%s",
                           i // batch_size + 1, resp.success, len(resp.text or ""), resp.error)

        # Fallback: if LLM returned nothing for this batch, create entries from snippets
        if not entries:
            for r in batch:
                content = r.get("content", "")
                if len(content) > 50:
                    fallback_entry = {
                        "title": r.get("title", "")[:100],
                        "content": content,
                        "tags": "",
                        "source": r.get("url", ""),
                        "confidence_score": 0.5,
                        "source_type": "community",
                        "facet": r.get("facet", ""),
                    }
                    all_entries.append(fallback_entry)
                    logger.info("extraction_fallback: title='%s' url='%s'", fallback_entry["title"], fallback_entry["source"])

    return all_entries


# ---------------------------------------------------------------------------
# Step 4: Gap analysis
# ---------------------------------------------------------------------------

GAP_SYSTEM = """You are a research coverage analyst. Given a topic, its facets, and
the knowledge entries collected so far, identify what's missing.

OUTPUT FORMAT (strict JSON, no markdown fences):
{
  "coverage_pct": 75,
  "covered_facets": ["facet1", "facet2"],
  "gap_facets": ["facet3"],
  "gap_queries": [
    {"query": "keyword search terms", "facet": "gap_facet", "priority": "high", "search_category": "general"}
  ],
  "assessment": "One paragraph on what's well covered and what's missing"
}"""


async def _analyze_gaps(
    state: ResearchState,
    model: str,
) -> dict:
    """Analyze coverage gaps in collected research."""
    entry_summaries = [
        f"[{e.get('facet', '?')}] {e.get('title', '')}: {e.get('content', '')[:100]}"
        for e in state.all_entries[-50:]  # Last 50 entries for context
    ]

    prompt = (
        f"Topic: {state.topic}\n"
        f"Expected facets: {', '.join(state.outline_facets)}\n"
        f"Entries collected: {len(state.all_entries)}\n"
        f"Iterations completed: {state.iteration}\n\n"
        f"Sample entries:\n" + "\n".join(entry_summaries[:30])
    )

    resp = await model_router.generate(
        prompt,
        model=model,
        system=GAP_SYSTEM,
        temperature=0.3,
        max_tokens=2048,
    )

    if resp.success:
        parsed = parse_json_object(resp.text)
        if parsed:
            return parsed

    return {
        "coverage_pct": 100,
        "covered_facets": state.outline_facets,
        "gap_facets": [],
        "gap_queries": [],
        "assessment": "Gap analysis failed — treating as complete.",
    }


# ---------------------------------------------------------------------------
# Step 5: Summary generation
# ---------------------------------------------------------------------------

SUMMARY_SYSTEM = """You are a research summarizer. Given collected knowledge entries,
produce a concise summary organized by facet/theme.

Write in clear prose paragraphs. Include key facts, numbers, and specifics.
Keep it under 500 words. No markdown headers — just flowing text with topic transitions."""


async def _generate_summary(
    state: ResearchState,
    model: str,
) -> str:
    """Generate a human-readable summary of all collected research."""
    entry_texts = [
        f"[{e.get('facet', '?')}] {e.get('content', '')}"
        for e in state.all_entries
    ]

    prompt = (
        f"Summarize the research collected on: {state.topic}\n\n"
        f"Total entries: {len(state.all_entries)}\n\n"
        + "\n".join(entry_texts[:60])
    )

    resp = await model_router.generate(
        prompt,
        model=model,
        system=SUMMARY_SYSTEM,
        temperature=0.3,
        max_tokens=2048,
    )

    if resp.success:
        return resp.text.strip()
    return f"Research collected {len(state.all_entries)} entries on '{state.topic}'."


# ---------------------------------------------------------------------------
# Main research loop (SSE streaming)
# ---------------------------------------------------------------------------

async def run_research(
    topic: str,
    depth: str = "medium",
    domain: str | None = None,
    model_overrides: dict | None = None,
) -> AsyncGenerator[str, None]:
    """Execute the full research loop, yielding SSE events."""
    t0 = time.monotonic()
    state = ResearchState(
        topic=topic,
        depth=depth,
        domain=domain or "eng",
    )

    decompose_model = get_model("model_verifier", model_overrides)
    extract_model = get_model("model_verifier", model_overrides)
    summary_model = get_model("model_verifier", model_overrides)

    yield _sse("research_started", {
        "topic": topic,
        "depth": depth,
        "domain": state.domain,
        "max_iterations": state.max_iterations,
    })

    # Initial decomposition
    decomposition = await _decompose_topic(topic, model=decompose_model)
    state.outline_facets = decomposition.get("facets", [topic])
    queries = decomposition.get("queries", [])

    yield _sse("decomposition_complete", {
        "complexity": decomposition.get("topic_complexity", "medium"),
        "facets": state.outline_facets,
        "query_count": len(queries),
    })

    # ---- Research loop ----
    while state.iteration < state.max_iterations:
        try:
            state.iteration += 1

            yield _sse("iteration_started", {
                "iteration": state.iteration,
                "query_count": len(queries),
            })

            # Search
            results = await _search_queries(queries, state)

            yield _sse("search_complete", {
                "iteration": state.iteration,
                "results_found": len(results),
                "total_urls": len(state.url_history),
            })

            if not results:
                yield _sse("iteration_complete", {
                    "iteration": state.iteration,
                    "entries_extracted": 0,
                    "entries_ingested": 0,
                    "reason": "no_results",
                })
                break

            # Extract (with heartbeat to keep SSE alive during long LLM calls)
            extract_task = asyncio.create_task(
                _extract_entries(results, topic, model=extract_model)
            )
            while not extract_task.done():
                await asyncio.sleep(8)
                if not extract_task.done():
                    yield _sse("heartbeat", {"status": "extracting", "iteration": state.iteration})
            entries = extract_task.result()

            yield _sse("extraction_complete", {
                "iteration": state.iteration,
                "entries_extracted": len(entries),
            })

            # Ingest
            ingested = 0
            if entries:
                pre_count = len(state.all_entries)
                state.all_entries.extend(entries)
                ingested = await ingest_entries(entries, domain=state.domain)
                state.total_ingested += ingested
                state.total_rejected += len(entries) - ingested

            yield _sse("ingestion_complete", {
                "iteration": state.iteration,
                "entries_ingested": ingested,
                "total_ingested": state.total_ingested,
                "total_rejected": state.total_rejected,
            })

            yield _sse("iteration_complete", {
                "iteration": state.iteration,
                "entries_extracted": len(entries),
                "entries_ingested": ingested,
            })

            # Convergence check: last iteration or no new entries
            if state.iteration >= state.max_iterations:
                break

            # Diminishing returns check
            if ingested == 0 and len(entries) > 0:
                yield _sse("convergence", {
                    "reason": "all_duplicates",
                    "message": "All extracted entries were duplicates — topic appears well covered.",
                })
                break

            # Gap analysis for next iteration (with heartbeat)
            gap_task = asyncio.create_task(_analyze_gaps(state, model=decompose_model))
            while not gap_task.done():
                await asyncio.sleep(8)
                if not gap_task.done():
                    yield _sse("heartbeat", {"status": "analyzing_gaps"})
            gaps = gap_task.result()
            coverage = gaps.get("coverage_pct", 100)
            state.covered_facets.update(gaps.get("covered_facets", []))

            yield _sse("gap_analysis", {
                "iteration": state.iteration,
                "coverage_pct": coverage,
                "covered_facets": list(state.covered_facets),
                "gap_facets": gaps.get("gap_facets", []),
                "assessment": gaps.get("assessment", ""),
            })

            # Check if well covered
            if coverage >= 85 and not gaps.get("gap_queries"):
                yield _sse("convergence", {
                    "reason": "coverage_threshold",
                    "coverage_pct": coverage,
                })
                break

            # Prepare next iteration queries from gaps
            queries = gaps.get("gap_queries", [])
            if not queries:
                break

        except Exception as exc:
            logger.error("research_loop_error: iteration=%d error=%s", state.iteration, exc, exc_info=True)
            yield _sse("error", {
                "message": f"Research iteration {state.iteration} failed: {exc}",
                "topic": topic,
            })
            return

    # ---- Final summary (with heartbeat) ----
    summary_task = asyncio.create_task(_generate_summary(state, model=summary_model))
    while not summary_task.done():
        await asyncio.sleep(8)
        if not summary_task.done():
            yield _sse("heartbeat", {"status": "summarizing"})
    summary = summary_task.result()
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    yield _sse("research_complete", {
        "topic": topic,
        "total_entries": len(state.all_entries),
        "total_ingested": state.total_ingested,
        "total_rejected": state.total_rejected,
        "iterations": state.iteration,
        "total_urls_searched": len(state.url_history),
        "total_queries": len(state.search_history),
        "duration_ms": elapsed_ms,
        "summary": summary,
        "domain": state.domain,
        "depth": depth,
    })
