"""Scaffold Engine — autonomous research agent.

Dispatches a research topic to one of five modes based on prefix:
- openapi:<url>      — OpenAPI/Swagger spec ingestion
- github:owner/repo  — GitHub repo docs + docstring ingestion
- http(s)://...      — single-URL direct ingestion
- (any other string) — topic mode: SearXNG-driven decompose/search/distill loop

PDF uploads go through run_research_pdf() (called from /research/pdf endpoint).

Topic-mode architecture: planner-executor loop with fan-out search / fan-in
extraction. Two-tier model strategy: model_verifier (7b) for decomposition/
extraction/summary; model_general (235b) is avoided in research loops
because of CPU-only serving constraints.

Pause/resume: topic-mode may pause on LLM-initiated clarifying question
(status='paused_awaiting_reply'). resume_research() re-enters the loop with
the user's reply injected as gap_focus into a fresh _decompose_topic() call.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import AsyncGenerator

import trafilatura

from app import model_router
from app.config import settings, get_model
from app.database import async_session
from app.modules.rag_pipeline import ingest_entries
from app.providers.base import Tool
from app.utils.http_clients import get_generic_http_client
from app.utils.llm_parsing import parse_json_array, parse_json_object  # noqa: F401 — kept for back-compat re-exports
from app.utils.tool_call_args import read_tool_args

# Re-exports for test patches and existing call sites — keeps
# `app.modules.research_agent.X` working after the 2026-05-05 split.
from app.modules.research_extractors import (
    DEFAULT_SOURCE_SCORE,
    _EXTRACT_BATCH_FULL_PAGE,
    _EXTRACT_BATCH_SNIPPET,
    SEARXNG_CACHE_TTL_SECONDS,
    _chunk_text,
    _detect_domain,
    _engines_for_category,
    _extract_page_title,
    _extract_pdf_text,
    _extract_pdfplumber,
    _extract_pypdf,
    _extract_threshold,
    _fetch_url_bounded,
    _is_arxiv_ref,
    _is_github_ref,
    _is_hf_ref,
    _is_hn_ref,
    _is_openapi_ref,
    _is_reddit_ref,
    _is_so_ref,
    _is_url,
    _is_wiki_ref,
    _parse_arxiv_ref,
    _parse_github_ref,
    _parse_hf_ref,
    _parse_hn_ref,
    _parse_openapi_ref,
    _parse_reddit_ref,
    _parse_so_ref,
    _parse_wiki_ref,
    _resolve_confidence,
    _robots_allowed,
    _score_source,
    _searxng_cache_get,
    _searxng_cache_key,
    _searxng_cache_set,
)
from app.modules.research_state import (
    HEARTBEAT_INTERVAL_SECONDS,
    SNAPSHOT_SCHEMA_VERSION,
    ResearchState,
    _atomic_claim_for_resume,
    _await_with_heartbeat,
    _build_snapshot,
    _finalize_session,
    _guard_and_create_session,
    _load_session_for_resume,
    _pause_session,
    _rehydrate_state,
    _run_with_session_lifecycle,
    _sse,
    _update_session_iteration,
)

logger = logging.getLogger("scaffold.research.agent")


# =============================================================================
# Contradiction check (sync — no await needed)
# =============================================================================

def _check_contradictions(entries: list[dict]) -> list[dict]:
    """Flag entry pairs whose titles share ≥2 words. Capped at 5."""
    contradictions: list[dict] = []
    for i, e1 in enumerate(entries):
        for e2 in entries[i + 1:]:
            t1 = e1.get("title", "")
            t2 = e2.get("title", "")
            if not t1 or not t2:
                continue
            shared = set(t1.lower().split()) & set(t2.lower().split())
            if len(shared) < 2:
                continue
            contradictions.append({
                "entry_a": t1,
                "entry_b": t2,
                "shared_concepts": sorted(shared),
            })
            if len(contradictions) >= 5:
                return contradictions[:5]
    return contradictions[:5]


# =============================================================================
# Web fetch + extract
# =============================================================================

async def _fetch_and_extract(results: list[dict]) -> list[dict]:
    """Fetch URLs concurrently, extract clean text via trafilatura.

    Returns [{"url", "content"}] for pages with ≥100 chars extracted.
    """
    if not results:
        return []

    sem = asyncio.Semaphore(settings.research_fetch_concurrency)
    urls = [r["url"] for r in results if r.get("url")]

    # Item 12 — shared persistent client; per-call timeout override.
    client = get_generic_http_client()

    async def _fetch_one(url: str) -> dict | None:
        async with sem:
            try:
                resp = await client.get(url, timeout=settings.research_fetch_timeout)
                if resp.status_code != 200 or not resp.text:
                    return None
                text_out = await asyncio.to_thread(
                    trafilatura.extract,
                    resp.text,
                    output_format="txt",
                    with_metadata=False,
                )
                if not text_out or len(text_out) < 100:
                    return None
                return {"url": url, "content": text_out}
            except Exception as e:
                logger.debug("trafilatura_fetch_failed: url=%s error=%s", url, e)
                return None

    fetched = await asyncio.gather(*[_fetch_one(u) for u in urls])

    return [f for f in fetched if f is not None]


# =============================================================================
# Prompts (V1 — bump suffix on prompt-level changes)
# =============================================================================

DECOMPOSE_SYSTEM_V1 = """You are a research planner. Decompose the given topic into
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

OUTPUT FORMAT (strict JSON, no markdown fences):
{
  "topic_complexity": "simple|medium|complex",
  "facets": ["facet1", "facet2", "facet3", "..."],
  "queries": [
    {"query": "keyword search terms", "facet": "which facet this covers", "priority": "high|medium|low", "search_category": "general"}
  ]
}"""

EXTRACT_SYSTEM_V1 = """You are a knowledge extraction engine. Given search results about a topic,
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

EXTRACT_PROMPT_V1 = """Extract factual knowledge entries from these search results about: {topic}

Search results:
---
{results}
---

Return ONLY the JSON array."""

GAP_SYSTEM_V1 = """You are a research coverage analyst. Given a topic, its facets, and
the knowledge entries collected so far, identify what's missing.

You may OPTIONALLY request user clarification if — and only if — a specific
ambiguity in the topic is actively blocking good coverage AND a one-sentence
answer from the user would materially change which queries to run next.
Do NOT pause for generic "would you like more detail" questions. Default to
no pause; only set needs_clarification=true when you have a concrete question.

OUTPUT FORMAT (strict JSON, no markdown fences):
{
  "coverage_pct": 75,
  "covered_facets": ["facet1", "facet2"],
  "gap_facets": ["facet3"],
  "gap_queries": [
    {"query": "keyword search terms", "facet": "gap_facet", "priority": "high", "search_category": "general"}
  ],
  "assessment": "One paragraph on what's well covered and what's missing",
  "needs_clarification": false,
  "clarifying_question": ""
}"""

SUMMARY_SYSTEM_V1 = """You are a research summarizer. Given collected knowledge entries,
produce a concise summary organized by facet/theme.

Write in clear prose paragraphs. Include key facts, numbers, and specifics.
Keep it under 500 words. No markdown headers — just flowing text with topic transitions."""


# Sprint W.6 — native tool-call schemas. The wrapper falls back to
# JSON-coaxing on providers without native tool support, so callers
# always read structured output via resp.tool_calls[0].arguments
# regardless of provider capability.

_QUERY_ITEM_SCHEMA = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "3-8 keyword search terms (NOT a natural-language question)"},
        "facet": {"type": "string", "description": "Which facet of the topic this query covers"},
        "priority": {"type": "string", "enum": ["high", "medium", "low"]},
        "search_category": {"type": "string", "enum": ["general", "news", "science", "it"]},
    },
    "required": ["query", "facet", "search_category"],
}

PLAN_RESEARCH_TOOL = Tool(
    name="plan_research",
    description="Decompose a research topic into facets and search queries.",
    input_schema={
        "type": "object",
        "properties": {
            "topic_complexity": {"type": "string", "enum": ["simple", "medium", "complex"]},
            "facets": {
                "type": "array",
                "items": {"type": "string"},
                "description": "3-8 distinct facets covering different aspects of the topic",
            },
            "queries": {
                "type": "array",
                "items": _QUERY_ITEM_SCHEMA,
                "description": "Searches to run for the topic; each query targets a single facet",
            },
        },
        "required": ["topic_complexity", "facets", "queries"],
    },
)

RECORD_ENTRIES_TOOL = Tool(
    name="record_entries",
    description="Record extracted factual knowledge entries from search results.",
    input_schema={
        "type": "object",
        "properties": {
            "entries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "content": {"type": "string", "description": "Self-contained factual statement"},
                        "tags": {"type": "string", "description": "Comma-separated tags"},
                        "source": {"type": "string", "description": "Source URL"},
                        "confidence_score": {"type": "number"},
                        "source_type": {
                            "type": "string",
                            "description": "tech_docs | news | community | official_docs | curated",
                        },
                        "facet": {"type": "string"},
                    },
                    "required": ["title", "content"],
                },
            },
        },
        "required": ["entries"],
    },
)

ASSESS_COVERAGE_TOOL = Tool(
    name="assess_coverage",
    description="Report which facets of the topic are covered, what's missing, and any gap queries to run next.",
    input_schema={
        "type": "object",
        "properties": {
            "coverage_pct": {"type": "integer", "minimum": 0, "maximum": 100},
            "covered_facets": {"type": "array", "items": {"type": "string"}},
            "gap_facets": {"type": "array", "items": {"type": "string"}},
            "gap_queries": {"type": "array", "items": _QUERY_ITEM_SCHEMA},
            "assessment": {"type": "string", "description": "One paragraph on coverage state"},
            "needs_clarification": {"type": "boolean"},
            "clarifying_question": {"type": "string"},
        },
        "required": ["coverage_pct", "covered_facets", "gap_facets"],
    },
)


# =============================================================================
# Decomposition, search, extraction, gap analysis, summary
# =============================================================================

async def _decompose_topic(
    topic: str,
    *,
    role: str = "model_verifier",
    overrides: dict | None = None,
    existing_facets: list | None = None,
    gap_focus: str | None = None,
) -> dict:
    """Decompose topic into queries. Retries once on <2 facets; falls back.

    §17.89 Pattern 3 — dispatch via role= so MODEL_VERIFIER_PROVIDER is honored.
    """
    prompt = f"Decompose this research topic into search queries:\n\nTOPIC: {topic}"
    if existing_facets:
        prompt += f"\n\nAlready covered facets (do NOT repeat): {', '.join(existing_facets)}"
    if gap_focus:
        prompt += f"\n\nFocus specifically on these gaps: {gap_focus}"

    resp = await model_router.tool_call(
        messages=[
            {"role": "system", "content": DECOMPOSE_SYSTEM_V1},
            {"role": "user", "content": prompt},
        ],
        tools=[PLAN_RESEARCH_TOOL],
        role=role, overrides=overrides, temperature=0.4, max_tokens=2048,
    )
    parsed = read_tool_args(resp)
    if parsed and "queries" in parsed:
        facets = parsed.get("facets", [])
        if len(facets) >= 2:
            return parsed
        logger.info("decomposition_retry: got %d facets, retrying", len(facets))
        retry_prompt = (
            f"Decompose this research topic into search queries:\n\n"
            f"TOPIC: {topic}\n\n"
            f"IMPORTANT: Break into at least 3 distinct subtopics. "
            f"Your previous attempt only produced {len(facets)} facet(s). "
            f"Each facet must cover a DIFFERENT aspect of the topic."
        )
        retry_resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": DECOMPOSE_SYSTEM_V1},
                {"role": "user", "content": retry_prompt},
            ],
            tools=[PLAN_RESEARCH_TOOL],
            role=role, overrides=overrides, temperature=0.5, max_tokens=2048,
        )
        retry_parsed = read_tool_args(retry_resp)
        if retry_parsed and "queries" in retry_parsed:
            return retry_parsed

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


async def _search_queries(
    queries: list[dict],
    state: ResearchState,
) -> list[dict]:
    """Run SearXNG searches with URL + case-insensitive query dedup."""
    from app.utils.http_clients import get_searxng_client

    all_results = []
    client = get_searxng_client()

    for q in queries[:settings.research_max_queries]:
        query_text = (q["query"] or "").strip()
        query_key = query_text.lower()
        if not query_key or query_key in state.search_history:
            continue

        cached = await _searxng_cache_get(query_text)
        if cached is not None:
            logger.info("searxng_cache_hit: query=%s results=%d", query_text, len(cached))
            for r in cached:
                url = r.get("url", "")
                if url and url not in state.url_history:
                    state.url_history.add(url)
                    all_results.append({
                        "title": r.get("title", ""),
                        "url": url,
                        "content": r.get("content", ""),
                        "facet": q.get("facet", ""),
                    })
            state.search_history.add(query_key)
            continue

        try:
            resp = await client.get(
                "/search",
                params={
                    "q": query_text,
                    "format": "json",
                    "categories": q.get("search_category", "general"),
                    "engines": _engines_for_category(q.get("search_category", "general")),
                },
            )
            if resp.status_code == 200:
                results = resp.json().get("results", [])[:10]
                await _searxng_cache_set(query_text, results)
                logger.info("searxng_cache_miss: query=%s results=%d", query_text, len(results))
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
            state.search_history.add(query_key)
        except Exception as e:
            logger.warning("research_search_failed: query='%s' error=%s", query_text, e)

        await asyncio.sleep(settings.research_searxng_delay)

    return all_results[:settings.research_max_urls_per_iteration]


async def _extract_entries(
    results: list[dict],
    topic: str,
    *,
    role: str = "model_verifier",
    overrides: dict | None = None,
) -> list[dict]:
    """Distill search results into knowledge entries.

    Fetches full pages via trafilatura; chunks long pages; snippet fallback.
    §17.89 Pattern 3 — dispatch via role= so MODEL_VERIFIER_PROVIDER is honored.
    """
    if not results:
        return []

    fetched = await _fetch_and_extract(results)
    url_to_text: dict[str, str] = {f["url"]: f["content"] for f in fetched}
    if fetched:
        logger.info(
            "research_fetch: %d/%d URLs extracted via trafilatura",
            len(fetched), len(results),
        )
    else:
        logger.warning("research_fetch: trafilatura returned nothing; snippet fallback")

    # §17.113 — classifier-driven distill bypass. URLs that classify_url
    # recognizes as a curated source (SO answer, HF model card, arXiv, GH
    # release/CI/test, Wikipedia, etc.) skip the 7b LLM extract pass:
    # chunks of the fetched body are ingested directly with the classified
    # source_type and §17.104 provenance. Non-curated URLs (and unfetched
    # ones) fall through to the existing LLM batch loop unchanged.
    from app.utils.url_classifier import classify_url, should_distill
    from app.modules.provenance import build_provenance

    all_entries: list[dict] = []
    expanded_results: list[dict] = []
    bypass_url_count = 0
    bypass_entry_count = 0
    for r in results:
        url = r.get("url", "")
        full = url_to_text.get(url)
        classified = classify_url(url)
        is_bypass = (
            classified is not None
            and not should_distill(classified)
            and bool(full)
        )
        if is_bypass:
            bypass_url_count += 1
            for chunk in _chunk_text(full):
                if len(chunk) > 50:
                    all_entries.append({
                        "title": r.get("title", "")[:100],
                        "content": chunk,
                        "tags": "",
                        "source": url,
                        "source_type": classified,
                        "facet": r.get("facet", ""),
                        "provenance": build_provenance(source_ref=url),
                    })
                    bypass_entry_count += 1
        elif full:
            for chunk in _chunk_text(full):
                expanded_results.append({
                    "title": r.get("title", ""),
                    "url": url,
                    "content": chunk,
                    "facet": r.get("facet", ""),
                })
        else:
            expanded_results.append(r)

    if bypass_url_count > 0:
        logger.info(
            "topic_classifier_bypass: bypassed_urls=%d bypassed_entries=%d distill_urls=%d",
            bypass_url_count, bypass_entry_count,
            len({r.get("url") for r in expanded_results if r.get("url")}),
        )

    batch_size = _EXTRACT_BATCH_FULL_PAGE if fetched else _EXTRACT_BATCH_SNIPPET

    for i in range(0, len(expanded_results), batch_size):
        batch = expanded_results[i:i + batch_size]
        results_text = "\n\n".join(
            f"Title: {r['title']}\nURL: {r['url']}\nSnippet: {r['content'][:600]}"
            for r in batch
        )
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_V1},
                {"role": "user", "content": EXTRACT_PROMPT_V1.format(topic=topic, results=results_text)},
            ],
            tools=[RECORD_ENTRIES_TOOL],
            role=role, overrides=overrides, temperature=0.1, max_tokens=1024,
        )

        entries: list[dict] = []
        parsed_args = read_tool_args(resp)
        if parsed_args and isinstance(parsed_args.get("entries"), list):
            entries = [e for e in parsed_args["entries"] if isinstance(e, dict)]
            if entries:
                for entry in entries:
                    src_url = entry.get("source", "")
                    if src_url:
                        entry["confidence_score"] = _resolve_confidence(
                            entry.get("confidence_score"), src_url,
                        )
                all_entries.extend(entries)
                logger.info(
                    "extraction_batch: %d entries from batch %d",
                    len(entries), i // batch_size + 1,
                )
            else:
                logger.warning(
                    "extraction_parse_failed: batch=%d raw_len=%d",
                    i // batch_size + 1, len(resp.text),
                )
        else:
            logger.warning(
                "extraction_llm_failed: batch=%d success=%s error=%s",
                i // batch_size + 1, resp.success,
                resp.error if resp else "no-resp",
            )

        if not entries:
            for r in batch:
                content = r.get("content", "")
                if len(content) > 50:
                    all_entries.append({
                        "title": r.get("title", "")[:100],
                        "content": content,
                        "tags": "",
                        "source": r.get("url", ""),
                        "confidence_score": _score_source(r.get("url", "")),
                        "source_type": "community",
                        "facet": r.get("facet", ""),
                    })
                    logger.info("extraction_fallback: url='%s'", r.get("url", ""))

    return all_entries


async def _analyze_gaps(
    state: ResearchState,
    *,
    role: str = "model_verifier",
    overrides: dict | None = None,
) -> dict:
    """Analyze coverage gaps. Retries once on parse failure.

    Item 7 — On parse failure, return ``coverage_pct=0`` with an explicit
    ``reason="gap_analysis_failed"``. The convergence check in
    ``_execute_iteration_loop`` must NOT treat this as 100% coverage; a
    failed gap analysis should not silently terminate the run early.

    Item 10 — Sample the last 30 entries directly (``[-30:]``), not
    ``[-50:][:30]`` which drops entries ``-50..-31`` before re-slicing.
    """
    entry_summaries = [
        f"[{e.get('facet', '?')}] {e.get('title', '')}: {e.get('content', '')[:100]}"
        for e in state.all_entries[-30:]
    ]

    prompt = (
        f"Topic: {state.topic}\n"
        f"Expected facets: {', '.join(state.outline_facets)}\n"
        f"Entries collected: {len(state.all_entries)}\n"
        f"Iterations completed: {state.iteration}\n\n"
        f"Sample entries:\n" + "\n".join(entry_summaries)
    )

    for attempt in range(2):
        resp = await model_router.tool_call(
            messages=[
                {"role": "system", "content": GAP_SYSTEM_V1},
                {"role": "user", "content": prompt},
            ],
            tools=[ASSESS_COVERAGE_TOOL],
            role=role, overrides=overrides, temperature=0.3, max_tokens=2048,
        )
        parsed = read_tool_args(resp)
        if parsed:
            return parsed
        if attempt == 0:
            logger.info("gap_analysis_retry: attempt 1 failed, retrying")

    logger.warning(
        "gap_analysis_failed: topic=%s iter=%s entries=%d — "
        "returning coverage_pct=0 so convergence does not trip",
        state.topic, state.iteration, len(state.all_entries),
    )
    return {
        "coverage_pct": 0,
        "covered_facets": sorted(state.covered_facets),
        "gap_facets": [f for f in state.outline_facets if f not in state.covered_facets],
        "gap_queries": [],
        "assessment": "Gap analysis failed to parse; continuing iteration.",
        "reason": "gap_analysis_failed",
    }


async def _generate_summary(
    state: ResearchState,
    *,
    role: str = "model_verifier",
    overrides: dict | None = None,
) -> str:
    """Generate human-readable summary of all collected research.

    §17.89 Pattern 3 — dispatch via role= so MODEL_VERIFIER_PROVIDER is honored.
    """
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
        prompt, role=role, overrides=overrides, system=SUMMARY_SYSTEM_V1,
        temperature=0.3, max_tokens=2048,
    )

    if resp.success:
        return resp.text.strip()
    return f"Research collected {len(state.all_entries)} entries on '{state.topic}'."


# =============================================================================
# Canonical research_complete payload (#141)
# =============================================================================

def _build_research_complete_payload(
    state: ResearchState,
    session_id: str,
    *,
    mode: str,
    duration_ms: int,
    topic: str | None = None,
    summary: str | None = None,
    **mode_extras,
) -> dict:
    """Common keys across all modes + mode-specific extras."""
    payload = {
        "session_id": session_id,
        "topic": topic if topic is not None else state.topic,
        "mode": mode,
        "domain": state.domain,
        "depth": state.depth,
        "duration_ms": duration_ms,
        "iterations": state.iteration,
        "total_entries": len(state.all_entries),
        "total_ingested": state.total_ingested,
        "new": state.total_new,
        "versioned": state.total_versioned,
        "rejected": state.total_rejected,
        "skipped_hash": state.total_skipped_hash,
        "total_urls_searched": len(state.url_history),
        "total_queries": len(state.search_history),
    }
    if summary is not None:
        payload["summary"] = summary
    payload.update(mode_extras)
    return payload


# =============================================================================
# Iteration loop (shared by run_research + resume_research, #50)
# =============================================================================

async def _execute_iteration_loop(
    state: ResearchState,
    session_id: str,
    initial_queries: list[dict],
    *,
    overrides: dict | None,
    topic: str,
    allow_pause: bool,
) -> AsyncGenerator[str, None]:
    """Search → extract → contradictions → ingest → gap-analysis loop.

    Mutates state in place. Sets state.paused=True if the gap analyzer
    requested clarification (only when allow_pause=True). Does NOT emit
    research_started or research_complete — caller owns those.
    """
    queries = initial_queries
    coverage: float | None = None

    while state.iteration < state.max_iterations:
        state.iteration += 1
        yield _sse("iteration_started", {
            "iteration": state.iteration,
            "query_count": len(queries),
        })

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

        extract_task = asyncio.create_task(
            _extract_entries(results, topic, overrides=overrides)
        )
        async for hb in _await_with_heartbeat(
            extract_task, {"status": "extracting", "iteration": state.iteration}
        ):
            yield hb
        entries = extract_task.result()

        yield _sse("extraction_complete", {
            "iteration": state.iteration,
            "entries_extracted": len(entries),
        })

        if entries:
            contradictions = _check_contradictions(entries)
            if contradictions:
                yield _sse("contradictions_detected", {
                    "count": len(contradictions),
                    "pairs": contradictions,
                })

        ingested = 0
        if entries:
            state.all_entries.extend(entries)
            stats = await ingest_entries(entries, domain=state.domain, session_id=session_id)
            ingested = stats["new"] + stats["versioned"]
            state.total_new += stats["new"]
            state.total_versioned += stats["versioned"]
            state.total_rejected += stats["rejected"]
            state.total_skipped_hash += stats["skipped_hash"]
            state.total_ingested += ingested

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

        await _update_session_iteration(session_id, state, coverage)

        if state.iteration >= state.max_iterations:
            break

        if ingested == 0 and len(entries) > 0:
            yield _sse("convergence", {
                "reason": "all_duplicates",
                "message": "All extracted entries were duplicates.",
            })
            break

        gap_task = asyncio.create_task(_analyze_gaps(state, overrides=overrides))
        async for hb in _await_with_heartbeat(
            gap_task, {"status": "analyzing_gaps"}
        ):
            yield hb
        gaps = gap_task.result()
        coverage = gaps.get("coverage_pct", 0)
        state.covered_facets.update(gaps.get("covered_facets", []))

        yield _sse("gap_analysis", {
            "iteration": state.iteration,
            "coverage_pct": coverage,
            "covered_facets": list(state.covered_facets),
            "gap_facets": gaps.get("gap_facets", []),
            "assessment": gaps.get("assessment", ""),
        })

        if (
            allow_pause
            and state.iteration < state.max_iterations
            and gaps.get("needs_clarification") is True
            and (gaps.get("clarifying_question") or "").strip()
        ):
            question = gaps["clarifying_question"].strip()
            await _pause_session(session_id, state, question)
            state.paused = True
            yield _sse("awaiting_reply", {
                "session_id": session_id,
                "question": question,
                "topic": topic,
                "iteration": state.iteration,
                "expires_in_seconds": 3600,
            })
            return

        if coverage >= 85 and not gaps.get("gap_queries"):
            yield _sse("convergence", {
                "reason": "coverage_threshold",
                "coverage_pct": coverage,
            })
            break

        queries = gaps.get("gap_queries", [])
        if not queries:
            break


# =============================================================================
# Shared direct-mode finalizer (#51, #55, #56, #57)
# =============================================================================

async def _ingest_and_finalize_direct(
    *,
    state: ResearchState,
    session_id: str,
    entries: list[dict],
    mode: str,
    topic: str,
    t0: float,
    summary_overrides: dict | None = None,
    summarize: bool = False,
    extra_complete_fields: dict | None = None,
) -> AsyncGenerator[str, None]:
    """Truncate → ingest → (optional summary) → finalize. Caller already emitted
    `extraction_complete`. Populates state.all_entries (#56). Emits
    content_truncated (#57), ingestion_complete, iteration_complete,
    research_complete with unified payload (#141)."""
    max_chars = settings.research_max_entry_chars
    truncated_count = 0
    for e in entries:
        content = e.get("content") or ""
        if len(content) > max_chars:
            e["content"] = content[:max_chars]
            truncated_count += 1

    if truncated_count > 0:
        yield _sse("content_truncated", {
            "count": truncated_count,
            "max_chars": max_chars,
            "mode": mode,
        })

    state.all_entries.extend(entries)

    # Audit Finding B fix — wrap the ingest_entries await in a heartbeat
    # generator so the SSE stream stays alive during embed + Milvus upsert.
    # Pre-fix the stream went silent for the entire ingest phase; if Ollama
    # wedged on the embedder runner, consumers saw 30 minutes of silence
    # before curl timed out at the orchestrator's 1800s local_timeout.
    # Heartbeats here let the operator see "ingesting" status and surface
    # an Ollama hang via `make logs-research` instead of a black-box stall.
    ingested = 0
    if entries:
        ingest_task = asyncio.create_task(
            ingest_entries(entries, domain=state.domain, session_id=session_id)
        )
        async for hb in _await_with_heartbeat(
            ingest_task,
            {"status": "ingesting", "iteration": state.iteration, "entries": len(entries)},
        ):
            yield hb
        stats = ingest_task.result()
        state.total_new += stats.get("new", 0)
        state.total_versioned += stats.get("versioned", 0)
        state.total_rejected += stats.get("rejected", 0)
        state.total_skipped_hash += stats.get("skipped_hash", 0)
        ingested = stats.get("new", 0) + stats.get("versioned", 0)
        state.total_ingested = state.total_new + state.total_versioned

    yield _sse("ingestion_complete", {
        "iteration": state.iteration,
        "entries_ingested": ingested,
        "total_ingested": state.total_ingested,
        "total_rejected": state.total_rejected,
        "new": state.total_new,
        "versioned": state.total_versioned,
        "rejected": state.total_rejected,
        "skipped_hash": state.total_skipped_hash,
    })

    yield _sse("iteration_complete", {
        "iteration": state.iteration,
        "entries_extracted": len(entries),
        "entries_ingested": ingested,
    })

    summary: str | None = None
    if summarize and state.all_entries:
        summary_task = asyncio.create_task(
            _generate_summary(state, overrides=summary_overrides)
        )
        async for hb in _await_with_heartbeat(
            summary_task, {"status": "summarizing"}
        ):
            yield hb
        summary = summary_task.result()

    duration_ms = int((time.monotonic() - t0) * 1000)
    await _update_session_iteration(session_id, state)
    await _finalize_session(session_id, "completed", duration_ms, summary)

    yield _sse("research_complete", _build_research_complete_payload(
        state, session_id,
        mode=mode,
        duration_ms=duration_ms,
        topic=topic,
        summary=summary,
        **(extra_complete_fields or {}),
    ))


# =============================================================================
# Direct modes: OpenAPI / GitHub / URL / PDF
# =============================================================================

async def _run_research_openapi_mode(
    spec_url: str,
    state: ResearchState,
    session_id: str,
    t0: float,
) -> AsyncGenerator[str, None]:
    """OpenAPI-mode: fetch + validate spec, ingest one entry per endpoint."""
    from app.utils.openapi_ingest import fetch_and_parse_spec

    state.outline_facets = ["openapi_spec"]
    state.iteration = 1
    yield _sse("decomposition_complete", {
        "complexity": "direct",
        "facets": state.outline_facets,
        "query_count": 0,
    })
    yield _sse("iteration_started", {
        "iteration": 1,
        "query_count": 0,
        "mode": "openapi",
    })

    task = asyncio.create_task(fetch_and_parse_spec(spec_url))
    async for hb in _await_with_heartbeat(
        task, {"status": "fetching_openapi", "iteration": 1}
    ):
        yield hb
    endpoints, meta = task.result()

    if not endpoints:
        raise RuntimeError(f"No endpoints found in spec at {spec_url}")

    yield _sse("search_complete", {
        "iteration": 1,
        "results_found": len(endpoints),
        "total_urls": 1,
        "mode": "openapi",
        "spec_title": meta["title"],
        "spec_version": meta["spec_version"],
        "openapi_version": meta["version"],
        "truncated": meta["truncated"],
    })

    state.url_history.add(spec_url)
    state.search_history.add(f"openapi:{spec_url}".lower())

    entries: list[dict] = []
    for ep in endpoints:
        source_url = f"{spec_url}#{ep['method']} {ep['path']}"
        tags = ep.get("tags") or []
        primary_facet = tags[0] if tags else "openapi_spec"
        entries.append({
            "title": ep["title"],
            "content": ep["content"],
            "source": source_url,
            "source_type": "tech_docs",
            "confidence_score": 0.95,
            "facet": primary_facet,
            "domain_tags": tags,
        })

    yield _sse("extraction_complete", {
        "iteration": 1,
        "entries_extracted": len(entries),
        "mode": "openapi",
    })

    async for evt in _ingest_and_finalize_direct(
        state=state,
        session_id=session_id,
        entries=entries,
        mode="openapi",
        topic=f"openapi:{spec_url}",
        t0=t0,
        extra_complete_fields={
            "spec_title": meta["title"],
            "spec_version": meta["spec_version"],
            "openapi_version": meta["version"],
            "endpoints_found": meta["total_endpoints"],
            "endpoints_ingested": meta["ingested_endpoints"],
            "truncated": meta["truncated"],
        },
    ):
        yield evt


async def _run_research_github_mode(
    owner: str,
    repo: str,
    state: ResearchState,
    session_id: str,
    t0: float,
    ref_hint: str | None = None,
) -> AsyncGenerator[str, None]:
    """GitHub-mode: deep fetch of repo content + release notes + issues/PRs.

    ``ref_hint=None`` → default branch (back-compat). Any other value
    (tag, branch, SHA) resolves to a commit SHA, locking every entry's
    provenance to that immutable ref.
    """
    from app.utils.github_ingest import (
        fetch_repo_content,
        fetch_repo_issues_and_prs,
        fetch_repo_releases,
    )
    from app.modules.provenance import build_provenance
    from app.config import settings as _settings

    state.outline_facets = ["github_repo"]
    state.iteration = 1
    yield _sse("decomposition_complete", {
        "complexity": "direct",
        "facets": state.outline_facets,
        "query_count": 0,
    })
    yield _sse("iteration_started", {
        "iteration": 1,
        "query_count": 0,
        "mode": "github",
        "ref_hint": ref_hint,
    })

    task = asyncio.create_task(fetch_repo_content(owner, repo, ref_hint=ref_hint))
    async for hb in _await_with_heartbeat(
        task, {"status": "fetching_github", "iteration": 1}
    ):
        yield hb
    files = task.result()

    # §17.110 — surface the resolved ref so the UI can show "v1.2.3 → abc123def…".
    # For ref_hint=None this is the default-branch name (weakly immutable).
    if files:
        yield _sse("source_ref_resolved", {
            "iteration": 1,
            "mode": "github",
            "ref_hint": ref_hint,
            "resolved_ref": files[0].get("source_ref", ""),
        })

    # Release notes + issues/PRs run after the main tree walk so a tree
    # failure doesn't mask them, and so an empty tree result can still
    # surface them as content.
    releases: list[dict] = []
    issues: list[dict] = []
    try:
        releases = await fetch_repo_releases(owner, repo, _settings.github_max_releases)
    except Exception as exc:
        logger.warning("github_releases_fetch_failed: %s/%s err=%s", owner, repo, exc)
    try:
        issues = await fetch_repo_issues_and_prs(
            owner, repo,
            _settings.github_max_issues,
            _settings.github_min_issue_reactions,
        )
    except Exception as exc:
        logger.warning("github_issues_fetch_failed: %s/%s err=%s", owner, repo, exc)

    all_items = list(files) + releases + issues
    if not all_items:
        raise RuntimeError(f"No ingestible content found in {owner}/{repo}")

    yield _sse("search_complete", {
        "iteration": 1,
        "results_found": len(all_items),
        "total_urls": len(all_items),
        "mode": "github",
        "files": len(files),
        "releases": len(releases),
        "issues": len(issues),
    })

    entries: list[dict] = []
    for f in all_items:
        source_url = f.get("source_url", "")
        if source_url:
            state.url_history.add(source_url)
        entries.append({
            "title": f"{owner}/{repo}: {f['path']}",
            "content": f["content"],
            "source": source_url,
            "source_type": f.get("source_type", "tech_docs"),
            # No confidence_score key → §17.104 derives from source_type.
            "facet": "github_repo",
            "provenance": build_provenance(
                source_ref=f.get("source_ref", ""),
                quality_signal=f.get("quality_signal", {}),
            ),
        })

    yield _sse("extraction_complete", {
        "iteration": 1,
        "entries_extracted": len(entries),
        "mode": "github",
    })

    # Note: github mode does no LLM extraction (entries are pulled
    # directly from the GitHub API as README + docs/*.md + top-level
    # docstrings), so there's no extract model to unload here. Audit
    # Finding C only applies to modes whose extract phase loads a
    # ~5 GB model that must be freed before the embedder cold-loads.

    async for evt in _ingest_and_finalize_direct(
        state=state,
        session_id=session_id,
        entries=entries,
        mode="github",
        topic=f"github:{owner}/{repo}",
        t0=t0,
        extra_complete_fields={"files_fetched": len(files)},
    ):
        yield evt


async def _run_research_hf_mode(
    kind: str,
    id_: str,
    state: ResearchState,
    session_id: str,
    t0: float,
) -> AsyncGenerator[str, None]:
    """HF-mode: fetch model_card / dataset_card / paper_abstract / space
    metadata, ingest with §17.104 provenance pinned to HF revision SHA.
    """
    from app.utils.hf_ingest import fetch_hf, HFNotFoundError, HFRateLimitError
    from app.modules.provenance import build_provenance

    state.outline_facets = [f"hf_{kind}"]
    state.iteration = 1
    yield _sse("decomposition_complete", {
        "complexity": "direct",
        "facets": state.outline_facets,
        "query_count": 0,
    })
    yield _sse("iteration_started", {
        "iteration": 1,
        "query_count": 0,
        "mode": "hf",
        "hf_kind": kind,
    })

    task = asyncio.create_task(fetch_hf(kind, id_))
    async for hb in _await_with_heartbeat(
        task, {"status": "fetching_hf", "iteration": 1}
    ):
        yield hb
    items = task.result()

    # §17.110 — emit resolved revision SHA / arXiv id for UI display.
    if items:
        yield _sse("source_ref_resolved", {
            "iteration": 1,
            "mode": "hf",
            "hf_kind": kind,
            "resolved_ref": items[0].get("source_ref", ""),
        })

    if not items:
        raise RuntimeError(f"No ingestible content found at hf:{kind}/{id_}")

    yield _sse("search_complete", {
        "iteration": 1,
        "results_found": len(items),
        "total_urls": len(items),
        "mode": "hf",
        "hf_kind": kind,
    })

    entries: list[dict] = []
    for it in items:
        source_url = it.get("source_url", "")
        if source_url:
            state.url_history.add(source_url)
        entries.append({
            "title": f"hf:{kind}/{id_}: {it['path']}",
            "content": it["content"],
            "source": source_url,
            "source_type": it.get("source_type", "tech_docs"),
            "facet": f"hf_{kind}",
            "provenance": build_provenance(
                source_ref=it.get("source_ref", ""),
                quality_signal=it.get("quality_signal", {}),
            ),
        })

    yield _sse("extraction_complete", {
        "iteration": 1,
        "entries_extracted": len(entries),
        "mode": "hf",
    })

    async for evt in _ingest_and_finalize_direct(
        state=state,
        session_id=session_id,
        entries=entries,
        mode="hf",
        topic=f"hf:{kind}/{id_}",
        t0=t0,
        extra_complete_fields={"hf_kind": kind, "items_fetched": len(items)},
    ):
        yield evt


async def _run_research_forum_mode(
    prefix: str,
    value: str,
    state: ResearchState,
    session_id: str,
    t0: float,
) -> AsyncGenerator[str, None]:
    """Forum-mode: SO / HN / arXiv. ``value`` is the post-prefix query.

    For arXiv we pack ``<mode>:<value>`` (e.g., ``id:2310.06825`` or
    ``query:transformer architecture``) into the ``value`` arg — the
    runner unpacks before dispatch. This avoids leaking a 3-arg dispatch
    through ResearchState.
    """
    from app.utils.forum_ingest import (
        fetch_arxiv,
        fetch_hn_items,
        fetch_reddit_posts,
        fetch_so_answers,
        fetch_wiki_pages,
    )
    from app.modules.provenance import build_provenance
    from app.config import settings as _settings

    state.outline_facets = [f"forum_{prefix}"]
    state.iteration = 1
    yield _sse("decomposition_complete", {
        "complexity": "direct",
        "facets": state.outline_facets,
        "query_count": 0,
    })
    yield _sse("iteration_started", {
        "iteration": 1,
        "query_count": 0,
        "mode": prefix,
    })

    # §17.110 — stats dict is populated by the gated fetchers (SO/HN/Reddit)
    # with fetched/kept/filtered_* counts. After fetch completes, emit a
    # `quality_gate_filtered` SSE so the UI can show "20 of 50 passed gates".
    fetch_stats: dict[str, int] = {}

    async def _do_fetch():
        if prefix == "so":
            return await fetch_so_answers(
                value, _settings.so_max_answers, _settings.so_min_score,
                stats=fetch_stats,
            )
        if prefix == "hn":
            return await fetch_hn_items(
                value, _settings.hn_max_items, _settings.hn_min_points,
                stats=fetch_stats,
            )
        if prefix == "arxiv":
            mode, val = value.split(":", 1)
            return await fetch_arxiv(mode, val, _settings.arxiv_max_sections)
        if prefix == "reddit":
            sub, q = value.split(":", 1)
            return await fetch_reddit_posts(
                sub, q, _settings.reddit_max_posts,
                _settings.reddit_min_score, _settings.reddit_min_comments,
                stats=fetch_stats,
            )
        if prefix == "wiki":
            return await fetch_wiki_pages(value, _settings.wiki_max_pages)
        raise ValueError(f"Unknown forum prefix: {prefix!r}")

    task = asyncio.create_task(_do_fetch())
    async for hb in _await_with_heartbeat(
        task, {"status": f"fetching_{prefix}", "iteration": 1}
    ):
        yield hb
    items = task.result()

    # Emit gate stats before checking emptiness so the UI sees the "why".
    if fetch_stats:
        yield _sse("quality_gate_filtered", {
            "iteration": 1,
            "mode": prefix,
            **fetch_stats,
        })

    if not items:
        raise RuntimeError(
            f"No ingestible content found for {prefix}:{value!r} "
            f"(quality gates may have filtered everything)"
        )

    yield _sse("search_complete", {
        "iteration": 1,
        "results_found": len(items),
        "total_urls": len(items),
        "mode": prefix,
    })

    entries: list[dict] = []
    for it in items:
        source_url = it.get("source_url", "")
        if source_url:
            state.url_history.add(source_url)
        entries.append({
            "title": f"{prefix}: {it['path']}",
            "content": it["content"],
            "source": source_url,
            "source_type": it.get("source_type", "tech_docs"),
            "facet": f"forum_{prefix}",
            "provenance": build_provenance(
                source_ref=it.get("source_ref", ""),
                quality_signal=it.get("quality_signal", {}),
            ),
        })

    yield _sse("extraction_complete", {
        "iteration": 1,
        "entries_extracted": len(entries),
        "mode": prefix,
    })

    async for evt in _ingest_and_finalize_direct(
        state=state,
        session_id=session_id,
        entries=entries,
        mode=prefix,
        topic=f"{prefix}:{value}",
        t0=t0,
        extra_complete_fields={"items_fetched": len(items)},
    ):
        yield evt


async def _unload_ollama_model(model: str) -> None:
    """Force-unload an Ollama model via ``keep_alive=0``.

    Audit Finding C (2026-05-09 follow-up to Finding B). On RAM-tight
    CPU-only hosts (~16 GB) the extract model (qwen2.5:7b, ~5 GB
    resident) and embedder (qwen3-embedding:8b, ~6 GB) loaded
    simultaneously trigger swap thrashing that wedges the embedder
    runner. The Ollama default 5-min keepalive guarantees both stay
    in memory while the embedder cold-loads — that's the squeeze.

    Calling this between the extract loop and the ingest/embed phase
    forces Ollama to free the extractor before the embedder runs.
    Failure is non-fatal: logged at warning, never raised. If the
    unload fails for any reason, Audit Finding B's bounded
    ``embed_timeout`` (120s × n_texts, capped 600s) still surfaces a
    subsequent wedge within minutes rather than the legacy 30 min.
    """
    if not model:
        return
    try:
        from app import model_router
        from app.config import settings
        client = model_router._get_client()
        await asyncio.wait_for(
            client.post(
                f"{settings.ollama_base_url}/api/generate",
                json={
                    "model": model,
                    "prompt": "",
                    "keep_alive": 0,
                    "stream": False,
                },
            ),
            timeout=15,
        )
        logger.info("ollama_model_unloaded: model=%s", model)
    except Exception as exc:
        logger.warning(
            "ollama_model_unload_failed: model=%s err=%s "
            "(embed may wedge under memory pressure)",
            model, exc,
        )


def _classify_extract_no_entries_reason(resp, parsed_args) -> str:
    """Audit Finding A — explain *why* a tool_call extract returned no entries.

    Pre-fix the URL/PDF extract sites logged ``…extract_failed: success=True
    error=None`` in the most common case (model returned 200 OK but didn't
    invoke the structured tool — typical W.6 brittleness on smaller CPU
    models). Operators couldn't tell that scenario from a real LLM failure.
    This helper returns a tight string distinguishing four cases:

      - ``no_response``                — wrapper returned None (defensive).
      - ``llm_error:<short>``          — actual dispatch / transport failure.
      - ``no_tool_calls``              — 200 OK but model produced no tool_calls
                                         (the W.6 case the fallback is for).
      - ``tool_args_missing_entries``  — tool was invoked but the args dict
                                         lacked the required ``entries`` key.
    """
    if resp is None:
        return "no_response"
    if not getattr(resp, "success", False):
        err = (getattr(resp, "error", "") or "")[:80]
        return f"llm_error:{err}" if err else "llm_error:unknown"
    if not parsed_args:
        return "no_tool_calls"
    return "tool_args_missing_entries"


async def _run_research_url_mode(
    url: str,
    state: ResearchState,
    session_id: str,
    *,
    overrides: dict | None,
    t0: float,
) -> AsyncGenerator[str, None]:
    """URL-mode: fetch one URL, extract via trafilatura, chunk, distill, ingest."""
    state.outline_facets = ["direct_url"]
    state.iteration = 1
    yield _sse("decomposition_complete", {
        "complexity": "direct",
        "facets": state.outline_facets,
        "query_count": 0,
    })
    yield _sse("iteration_started", {
        "iteration": 1,
        "query_count": 0,
        "mode": "direct_url",
    })

    if not await _robots_allowed(url):
        raise RuntimeError(f"robots.txt disallows fetching {url}")

    html = await _fetch_url_bounded(url)
    if not html:
        cap_mb = settings.research_max_url_bytes // (1024 * 1024)
        raise RuntimeError(
            f"Failed to fetch {url} (non-200 or exceeded {cap_mb}MB cap)"
        )

    text_content = await asyncio.to_thread(
        trafilatura.extract, html,
        output_format="txt", with_metadata=False,
    )
    if not text_content or len(text_content) < 100:
        raise RuntimeError(
            f"No extractable content at {url} "
            f"(got {len(text_content or '')} chars)"
        )

    page_title = await _extract_page_title(html, url)

    yield _sse("search_complete", {
        "iteration": 1,
        "results_found": 1,
        "total_urls": 1,
        "mode": "direct_url",
    })

    state.url_history.add(url)
    state.search_history.add(f"direct:{url}".lower())

    chunks = _chunk_text(text_content)
    prompt_topic = page_title if page_title != url[:200] else f"content at {url}"
    batch_size = _EXTRACT_BATCH_FULL_PAGE
    entries: list[dict] = []

    # §17.112 — distill bypass. URLs that classify to a curated source_type
    # (SO answer, HF model card, arXiv abstract, GH release notes/CI/tests,
    # Wikipedia, etc.) are already structured; the 7b extract pass adds
    # nothing. Build entries directly from chunks with the classified
    # source_type + §17.104 provenance; guard the LLM loop below so each
    # iteration is a cheap no-op when bypassed.
    from app.utils.url_classifier import classify_url, should_distill
    from app.modules.provenance import build_provenance

    classified_source_type = classify_url(url)
    distill_bypass = (
        classified_source_type is not None
        and not should_distill(classified_source_type)
    )
    if distill_bypass:
        yield _sse("distill_bypassed", {
            "iteration": 1,
            "source_type": classified_source_type,
            "url": url,
            "chunks": len(chunks),
        })
        for c in chunks:
            if len(c) > 50:
                entries.append({
                    "title": page_title,
                    "content": c,
                    "tags": "",
                    "source": url,
                    "source_type": classified_source_type,
                    "facet": "direct_url",
                    "provenance": build_provenance(source_ref=url),
                })
        logger.info(
            "url_mode_distill_bypassed: source_type=%s entries=%d url=%s",
            classified_source_type, len(entries), url,
        )

    # Audit Finding B — explicit batch counters so a future hang can be
    # localized via `make logs-research`. The pre-fix code logged nothing
    # between the first batch's warning and `extraction_complete`, so an
    # operator inspecting an indefinitely-running session had no signal
    # for which iteration was in flight.
    total_batches = (len(chunks) + batch_size - 1) // batch_size
    logger.info(
        "url_mode_extract_loop_start: chunks=%d batches=%d batch_size=%d url=%s bypass=%s",
        len(chunks), total_batches, batch_size, url, distill_bypass,
    )

    for i in range(0, len(chunks), batch_size):
        if distill_bypass:
            continue  # §17.112 — entries already built above; skip the LLM
        batch_idx = i // batch_size
        batch_chunks = chunks[i:i + batch_size]
        logger.info(
            "url_mode_extract_batch_start: batch=%d/%d chunks_in_batch=%d",
            batch_idx, total_batches, len(batch_chunks),
        )
        results_text = "\n\n".join(
            f"Title: {page_title}\nURL: {url}\nSnippet: {c[:600]}"
            for c in batch_chunks
        )
        task = asyncio.create_task(model_router.tool_call(
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_V1},
                {"role": "user", "content": EXTRACT_PROMPT_V1.format(topic=prompt_topic, results=results_text)},
            ],
            tools=[RECORD_ENTRIES_TOOL],
            role="model_verifier",
            overrides=overrides,
            temperature=0.1,
            max_tokens=1024,
        ))
        async for hb in _await_with_heartbeat(
            task, {"status": "extracting", "iteration": 1}
        ):
            yield hb
        resp = task.result()

        batch_entries: list[dict] = []
        parsed_args = read_tool_args(resp)
        if parsed_args and isinstance(parsed_args.get("entries"), list):
            batch_entries = [e for e in parsed_args["entries"] if isinstance(e, dict)]
            for entry in batch_entries:
                src_url = entry.get("source", "") or url
                entry["source"] = src_url
                entry["confidence_score"] = _resolve_confidence(
                    entry.get("confidence_score"), src_url,
                )
                entry["facet"] = "direct_url"
                entry.setdefault("source_type", "community")
            entries.extend(batch_entries)
        else:
            # Audit Finding A — distinguish "model declined to use the
            # structured tool" from a genuine LLM dispatch failure. Pre-
            # fix the warning logged "url_mode_extract_failed: success=True
            # error=None" with no signal for which path fell through.
            reason = _classify_extract_no_entries_reason(resp, parsed_args)
            logger.warning(
                "url_mode_extract_no_entries: batch=%d reason=%s falling_back_to_chunks",
                batch_idx, reason,
            )

        if not batch_entries:
            fallback_count = 0
            for c in batch_chunks:
                if len(c) > 50:
                    entries.append({
                        "title": page_title,
                        "content": c,
                        "tags": "",
                        "source": url,
                        "confidence_score": _score_source(url),
                        "source_type": "community",
                        "facet": "direct_url",
                    })
                    fallback_count += 1
            logger.info(
                "url_mode_extract_batch_done: batch=%d/%d "
                "entries_from_llm=0 entries_from_chunks=%d total_entries_so_far=%d",
                batch_idx, total_batches, fallback_count, len(entries),
            )
        else:
            logger.info(
                "url_mode_extract_batch_done: batch=%d/%d "
                "entries_from_llm=%d entries_from_chunks=0 total_entries_so_far=%d",
                batch_idx, total_batches, len(batch_entries), len(entries),
            )

    logger.info(
        "url_mode_extract_loop_complete: total_entries=%d batches=%d url=%s",
        len(entries), total_batches, url,
    )
    yield _sse("extraction_complete", {
        "iteration": 1,
        "entries_extracted": len(entries),
    })

    # Audit Finding C — unload extract model before embed loads. See
    # _unload_ollama_model docstring for context. §17.89 — resolve the model
    # name for the unload helper from the same role+overrides used above.
    await _unload_ollama_model(get_model("model_verifier", overrides))

    async for evt in _ingest_and_finalize_direct(
        state=state,
        session_id=session_id,
        entries=entries,
        mode="direct_url",
        topic=url,
        t0=t0,
        summary_overrides=overrides,
        summarize=True,
    ):
        yield evt


async def _run_research_pdf_mode(
    pdf_bytes: bytes,
    filename: str,
    extractor: str,
    state: ResearchState,
    session_id: str,
    *,
    overrides: dict | None,
    t0: float,
) -> AsyncGenerator[str, None]:
    """PDF-mode: extract bytes via pypdf (or plumber fallback), distill, ingest."""
    if len(pdf_bytes) > settings.research_max_pdf_bytes:
        cap_mb = settings.research_max_pdf_bytes // (1024 * 1024)
        raise RuntimeError(
            f"PDF exceeds {cap_mb}MB cap ({len(pdf_bytes)} bytes)"
        )

    state.outline_facets = ["direct_pdf"]
    state.iteration = 1
    yield _sse("decomposition_complete", {
        "complexity": "direct",
        "facets": state.outline_facets,
        "query_count": 0,
    })
    yield _sse("iteration_started", {
        "iteration": 1,
        "query_count": 0,
        "mode": "direct_pdf",
    })

    try:
        text_content, page_count, used, fell_back = await _extract_pdf_text(
            pdf_bytes, extractor=extractor,
        )
    except RuntimeError as e:
        raise RuntimeError(
            f"{e}. Scanned PDFs require OCR (not yet supported)."
        ) from e

    if fell_back:  # #60
        yield _sse("extractor_fallback", {
            "iteration": 1,
            "from": "pypdf",
            "to": "plumber",
            "reason": "insufficient_text",
        })

    yield _sse("search_complete", {
        "iteration": 1,
        "results_found": 1,
        "total_urls": 1,
        "mode": "direct_pdf",
        "page_count": page_count,
        "extractor_used": used,
        "char_count": len(text_content),
    })

    virtual_url = f"pdf://{filename}"
    state.url_history.add(virtual_url)
    state.search_history.add(f"direct_pdf:{filename}".lower())

    chunks = _chunk_text(text_content)
    batch_size = _EXTRACT_BATCH_FULL_PAGE
    entries: list[dict] = []
    pdf_default_confidence = 0.8  # local upload, reasonably trusted

    for i in range(0, len(chunks), batch_size):
        batch_chunks = chunks[i:i + batch_size]
        # #59: label clarifies meaning (total pages, not page marker)
        results_text = "\n\n".join(
            f"Title: {filename} (total pages: {page_count})\n"
            f"URL: {virtual_url}\nSnippet: {c[:600]}"
            for c in batch_chunks
        )
        task = asyncio.create_task(model_router.tool_call(
            messages=[
                {"role": "system", "content": EXTRACT_SYSTEM_V1},
                {"role": "user", "content": EXTRACT_PROMPT_V1.format(topic=filename, results=results_text)},
            ],
            tools=[RECORD_ENTRIES_TOOL],
            role="model_verifier",
            overrides=overrides,
            temperature=0.1,
            max_tokens=1024,
        ))
        async for hb in _await_with_heartbeat(
            task, {"status": "extracting", "iteration": 1}
        ):
            yield hb
        resp = task.result()

        batch_entries: list[dict] = []
        parsed_args = read_tool_args(resp)
        if parsed_args and isinstance(parsed_args.get("entries"), list):
            batch_entries = [e for e in parsed_args["entries"] if isinstance(e, dict)]
            for entry in batch_entries:
                entry["source"] = virtual_url
                llm_conf = entry.get("confidence_score")
                if (
                    isinstance(llm_conf, (int, float))
                    and 0.0 <= float(llm_conf) <= 1.0
                ):
                    entry["confidence_score"] = float(llm_conf)
                else:
                    if llm_conf is not None:
                        logger.warning(
                            "confidence_out_of_range: got=%s pdf=%s "
                            "falling_back_to=%.2f",
                            llm_conf, virtual_url, pdf_default_confidence,
                        )
                    entry["confidence_score"] = pdf_default_confidence
                entry["facet"] = "direct_pdf"
                entry["source_type"] = entry.get("source_type") or "tech_docs"
            entries.extend(batch_entries)
        else:
            # Audit Finding A — same wording fix as the URL-mode site.
            reason = _classify_extract_no_entries_reason(resp, parsed_args)
            logger.warning(
                "pdf_mode_extract_no_entries: batch=%d reason=%s falling_back_to_chunks",
                i // batch_size, reason,
            )

        if not batch_entries:
            for c in batch_chunks:
                if len(c) > 50:
                    entries.append({
                        "title": filename,
                        "content": c,
                        "tags": "",
                        "source": virtual_url,
                        "confidence_score": pdf_default_confidence,
                        "source_type": "tech_docs",
                        "facet": "direct_pdf",
                    })

    yield _sse("extraction_complete", {
        "iteration": 1,
        "entries_extracted": len(entries),
    })

    # Audit Finding C — unload extract model before embed loads. See
    # _unload_ollama_model docstring for context. §17.89 — resolve the
    # model name for unload from the same role+overrides used above.
    await _unload_ollama_model(get_model("model_verifier", overrides))

    async for evt in _ingest_and_finalize_direct(
        state=state,
        session_id=session_id,
        entries=entries,
        mode="direct_pdf",
        topic=filename,
        t0=t0,
        summary_overrides=overrides,
        summarize=True,
        extra_complete_fields={
            "page_count": page_count,
            "extractor_used": used,
        },
    ):
        yield evt


# =============================================================================
# Public entry points
# =============================================================================

async def run_research(
    topic: str,
    depth: str = "medium",
    domain: str | None = None,
    model_overrides: dict | None = None,
) -> AsyncGenerator[str, None]:
    """Execute research, yielding SSE events. Dispatches to direct modes by prefix.

    Lifecycle is delegated to ``_run_with_session_lifecycle`` so
    ``CancelledError`` from a disconnected SSE client transitions the
    session to ``cancelled`` (not stuck in ``running`` for 30 min).
    """
    t0 = time.monotonic()
    research_domain = domain or _detect_domain(topic)

    # Determine mode + persisted depth label
    if _is_openapi_ref(topic):
        mode, state_depth = "openapi", "direct_openapi"
    elif _is_github_ref(topic):
        mode, state_depth = "github", "direct_github"
    elif _is_hf_ref(topic):
        mode, state_depth = "hf", "direct_hf"
    elif _is_so_ref(topic):
        mode, state_depth = "so", "direct_so"
    elif _is_hn_ref(topic):
        mode, state_depth = "hn", "direct_hn"
    elif _is_arxiv_ref(topic):
        mode, state_depth = "arxiv", "direct_arxiv"
    elif _is_reddit_ref(topic):
        mode, state_depth = "reddit", "direct_reddit"
    elif _is_wiki_ref(topic):
        mode, state_depth = "wiki", "direct_wiki"
    elif _is_url(topic):
        mode, state_depth = "direct_url", "direct_url"
    else:
        mode, state_depth = "topic", depth

    session_id, existing = await _guard_and_create_session(
        topic, state_depth, research_domain,
    )
    if session_id is None:
        payload = {
            "message": (
                f"Research already in progress: '{existing['topic']}'"
                if existing else "Research already in progress"
            ),
            "http_status": 409,
        }
        if existing:
            payload["existing_session"] = str(existing["id"])
        yield _sse("error", payload)
        return

    state = ResearchState(topic=topic, depth=state_depth, domain=research_domain)

    # --- Direct modes: single-iteration ---
    if mode != "topic":
        async def _direct_inner():
            yield _sse("research_started", {
                "topic": topic,
                "depth": state_depth,
                "domain": state.domain,
                "max_iterations": 1,
                "session_id": session_id,
                "mode": mode,
            })
            if mode == "openapi":
                async for evt in _run_research_openapi_mode(
                    _parse_openapi_ref(topic), state, session_id, t0,
                ):
                    yield evt
            elif mode == "github":
                owner, repo, ref_hint = _parse_github_ref(topic)
                async for evt in _run_research_github_mode(
                    owner, repo, state, session_id, t0, ref_hint=ref_hint,
                ):
                    yield evt
            elif mode == "hf":
                kind, hf_id = _parse_hf_ref(topic)
                async for evt in _run_research_hf_mode(
                    kind, hf_id, state, session_id, t0,
                ):
                    yield evt
            elif mode in ("so", "hn", "arxiv", "reddit", "wiki"):
                if mode == "so":
                    value = _parse_so_ref(topic)
                elif mode == "hn":
                    value = _parse_hn_ref(topic)
                elif mode == "arxiv":
                    # arxiv keeps an (id|query) tuple — pack into a single string
                    # for the forum-mode runner; the runner re-parses to dispatch.
                    arx_mode, arx_val = _parse_arxiv_ref(topic)
                    value = f"{arx_mode}:{arx_val}"
                elif mode == "reddit":
                    # reddit: pack (subreddit, query) the same way; runner unpacks.
                    sub, q = _parse_reddit_ref(topic)
                    value = f"{sub}:{q}"
                else:  # wiki
                    value = _parse_wiki_ref(topic)
                async for evt in _run_research_forum_mode(
                    mode, value, state, session_id, t0,
                ):
                    yield evt
            elif mode == "direct_url":
                async for evt in _run_research_url_mode(
                    topic, state, session_id,
                    overrides=model_overrides, t0=t0,
                ):
                    yield evt

        async for evt in _run_with_session_lifecycle(
            session_id, _direct_inner, t0, topic,
        ):
            yield evt
        return

    # --- Topic mode (§17.89: pre-resolved model_* names dropped; helpers
    # route via role= and the same overrides dict.) ---
    async def _topic_inner():
        yield _sse("research_started", {
            "topic": topic,
            "depth": depth,
            "domain": state.domain,
            "max_iterations": state.max_iterations,
            "session_id": session_id,
        })

        decomposition = await _decompose_topic(topic, overrides=model_overrides)
        state.outline_facets = decomposition.get("facets", [topic])
        queries = decomposition.get("queries", [])

        yield _sse("decomposition_complete", {
            "complexity": decomposition.get("topic_complexity", "medium"),
            "facets": state.outline_facets,
            "query_count": len(queries),
        })

        async for evt in _execute_iteration_loop(
            state=state,
            session_id=session_id,
            initial_queries=queries,
            overrides=model_overrides,
            topic=topic,
            allow_pause=True,
        ):
            yield evt

        if state.paused:
            return

        # Item 8: summary failure must not fail the whole run.
        summary: str | None = None
        try:
            summary_task = asyncio.create_task(
                _generate_summary(state, overrides=model_overrides)
            )
            async for hb in _await_with_heartbeat(
                summary_task, {"status": "summarizing"},
            ):
                yield hb
            summary = summary_task.result()
        except Exception as summary_exc:
            logger.warning(
                "summary_failed: session=%s error=%s",
                session_id, summary_exc, exc_info=True,
            )
            yield _sse("warning", {
                "stage": "summary",
                "message": f"Summary generation failed: {summary_exc}",
                "session_id": session_id,
            })

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        await _update_session_iteration(session_id, state)
        await _finalize_session(session_id, "completed", elapsed_ms, summary)

        yield _sse("research_complete", _build_research_complete_payload(
            state, session_id,
            mode="topic",
            duration_ms=elapsed_ms,
            topic=topic,
            summary=summary,
        ))

    async for evt in _run_with_session_lifecycle(
        session_id, _topic_inner, t0, topic,
    ):
        yield evt


async def resume_research(
    session_id: str,
    user_reply: str,
    model_overrides: dict | None = None,
) -> AsyncGenerator[str, None]:
    """Resume paused session. Atomic claim prevents two concurrent resumers (#3)."""
    t0 = time.monotonic()

    row = await _load_session_for_resume(session_id)
    if row is None:
        yield _sse("error", {
            "message": f"Session not found: {session_id}",
            "http_status": 404,
        })
        return

    if row["status"] != "paused_awaiting_reply":
        yield _sse("error", {
            "message": f"Session is not awaiting reply (status={row['status']})",
            "session_id": session_id,
            "http_status": 409,
        })
        return

    expires = row.get("pause_expires_at")
    if expires is not None:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= expires:
            await _finalize_session(
                session_id, "cancelled",
                int((time.monotonic() - t0) * 1000),
                error_message="Pause expired before reply received",
            )
            yield _sse("error", {
                "message": "Pause expired; session cancelled",
                "session_id": session_id,
                "http_status": 410,
            })
            return

    reply = (user_reply or "").strip()
    if not reply:
        yield _sse("error", {
            "message": "Reply cannot be empty",
            "session_id": session_id,
            "http_status": 400,
        })
        return

    # Atomic claim — loses to any concurrent resumer on the same session
    claimed = await _atomic_claim_for_resume(session_id, reply)
    if not claimed:
        yield _sse("error", {
            "message": "Session was claimed by another resumer or changed status",
            "session_id": session_id,
            "http_status": 409,
        })
        return

    state = _rehydrate_state(row)
    topic = row["topic"]

    async def _resume_inner():
        yield _sse("research_resumed", {
            "session_id": session_id,
            "topic": topic,
            "iteration": state.iteration,
            "reply": reply,
        })

        # #142: targeted decompose with reply as gap_focus (replaces 2-query seed)
        decomposition = await _decompose_topic(
            topic,
            overrides=model_overrides,
            existing_facets=state.outline_facets,
            gap_focus=reply,
        )
        new_queries = decomposition.get("queries", [])

        yield _sse("decomposition_complete", {
            "complexity": decomposition.get("topic_complexity", "medium"),
            "facets": decomposition.get("facets", []),
            "query_count": len(new_queries),
            "resumed": True,
        })

        async for evt in _execute_iteration_loop(
            state=state,
            session_id=session_id,
            initial_queries=new_queries,
            overrides=model_overrides,
            topic=topic,
            allow_pause=False,
        ):
            yield evt

        # Item 8: summary failure must not fail the whole resume.
        summary: str | None = None
        try:
            summary_task = asyncio.create_task(
                _generate_summary(state, overrides=model_overrides)
            )
            async for hb in _await_with_heartbeat(
                summary_task, {"status": "summarizing"},
            ):
                yield hb
            summary = summary_task.result()
        except Exception as summary_exc:
            logger.warning(
                "resume_summary_failed: session=%s error=%s",
                session_id, summary_exc, exc_info=True,
            )
            yield _sse("warning", {
                "stage": "summary",
                "message": f"Summary generation failed: {summary_exc}",
                "session_id": session_id,
            })

        elapsed_ms = int((time.monotonic() - t0) * 1000)
        await _update_session_iteration(session_id, state)
        await _finalize_session(session_id, "completed", elapsed_ms, summary)

        yield _sse("research_complete", _build_research_complete_payload(
            state, session_id,
            mode="topic",
            duration_ms=elapsed_ms,
            topic=topic,
            summary=summary,
            resumed_from_pause=True,
        ))

    async for evt in _run_with_session_lifecycle(
        session_id, _resume_inner, t0, topic,
    ):
        yield evt


async def run_research_pdf(
    pdf_bytes: bytes,
    filename: str,
    extractor: str = "auto",
    domain: str | None = None,
    model_overrides: dict | None = None,
) -> AsyncGenerator[str, None]:
    """Entry point for PDF research (called from /research/pdf endpoint).

    Lifecycle is delegated to ``_run_with_session_lifecycle`` so client
    disconnect finalizes the session as ``cancelled`` instead of orphaning
    it in ``running`` until the 30-min reaper.
    """
    t0 = time.monotonic()
    research_domain = domain or _detect_domain(filename)

    session_id, existing = await _guard_and_create_session(
        filename, "direct_pdf", research_domain,
    )
    if session_id is None:
        payload = {
            "message": (
                f"Research already in progress: '{existing['topic']}'"
                if existing else "Research already in progress"
            ),
            "http_status": 409,
        }
        if existing:
            payload["existing_session"] = str(existing["id"])
        yield _sse("error", payload)
        return

    state = ResearchState(
        topic=filename, depth="direct_pdf", domain=research_domain,
    )

    async def _pdf_inner():
        yield _sse("research_started", {
            "topic": filename,
            "depth": "direct_pdf",
            "domain": state.domain,
            "max_iterations": 1,
            "session_id": session_id,
            "mode": "direct_pdf",
            "bytes": len(pdf_bytes),
        })
        async for evt in _run_research_pdf_mode(
            pdf_bytes=pdf_bytes,
            filename=filename,
            extractor=extractor,
            state=state,
            session_id=session_id,
            overrides=model_overrides,
            t0=t0,
        ):
            yield evt

    async for evt in _run_with_session_lifecycle(
        session_id, _pdf_inner, t0, filename,
    ):
        yield evt
