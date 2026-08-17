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
import re
import time
from datetime import datetime, timezone
from typing import AsyncGenerator

import trafilatura

from app import model_router
from app.config import settings, get_model
from app.database import async_session
from app.modules.rag_pipeline import ingest_entries
from app.providers.base import ModelResponse, Tool
# noqa: F401 — re-exported so research_extractors._fetch_url_bounded reaches it
# via _ra().get_generic_http_client(); also the patch target for url-mode tests.
from app.utils.http_clients import get_generic_http_client  # noqa: F401
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
    SEARXNG_FALLBACK_ENGINES,
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
    _touch_last_activity,
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

    async def _fetch_one(url: str) -> dict | None:
        async with sem:
            try:
                # §17.612 (audit #6) — route through the §17.93 hardened fetch
                # (SSRF pre/post-redirect host check + streamed research_max_url_bytes
                # cap) instead of a raw client.get whose full body buffered into
                # orchestrator RAM and whose follow_redirects could reach a
                # private/metadata IP. Keep the tighter topic-fetch timeout.
                html = await _fetch_url_bounded(url, timeout=settings.research_fetch_timeout)
                if not html:
                    return None
                text_out = await asyncio.to_thread(
                    trafilatura.extract,
                    html,
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
- Produce 5-12 distinct facets covering DIFFERENT aspects of the topic (break the topic into as many genuinely distinct sub-topics as it warrants)
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
extract atomic, self-contained factual entries and record them by calling the
`record_entries` tool.

Rules:
- Each entry is ONE fact that can be understood without surrounding context
- Be specific: include numbers, names, versions, dates where applicable
- Assign confidence: 1.0 = verified fact, 0.7 = secondary source, 0.4 = opinion/speculation
- Discard noise, opinions, marketing language
- 5-15 entries per batch
- Content must NOT contain escaped quotes or backslashes

Call `record_entries` with an `entries` array; each entry has:
- title: Short descriptive title
- content: Self-contained factual statement. Technically precise.
- tags: comma,separated,tags
- source: the URL the fact came from
- confidence_score: 0.0-1.0 (see the confidence rule above)
- source_type: one of tech_docs|news|community|official_docs|curated
- facet: which facet of the topic this covers

Respond ONLY by calling the tool — do not write a prose answer."""

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

CRITICAL: Summarize ONLY the provided entries. Do NOT add facts, names, numbers,
or claims that are not present in the entries — no outside or recalled knowledge.
If the entries are thin or off-topic, say so plainly rather than filling the gap
from memory. (This prevents the summary from bleeding unrelated training-data
content — e.g. a "kubernetes" run that drifted into a Svelte tutorial.)

Write in clear prose paragraphs. Include key facts, numbers, and specifics
DRAWN FROM THE ENTRIES. Keep it under 500 words. No markdown headers — just
flowing text with topic transitions."""


# §17.662 — after summarizing, decide whether the topic presents the user with a
# real DECISION between distinct viable approaches, and if so lay out the
# options tailored to their needs. The hard rule is ONLY-WHEN-APPLICABLE: a
# straightforward factual / single-answer topic ("what port does postgres use",
# "how does TLS work") has NO options — return has_options=false rather than
# manufacturing false choices. Tone mirrors the §17.654 assist decision nodes:
# suggest a default, framed explicitly as the user's call.
OPTIONS_SYSTEM_V1 = """You help a user turn research into a DECISION. Given a topic (and often a \
"User's goal / needs"), decide whether the user genuinely faces a meaningful CHOICE \
— one where reasonable people would pick differently and it materially changes what \
they do next.

THE TEST for a real decision: the options must lead to MATERIALLY DIFFERENT \
outcomes the operator would actually weigh — different data safety, cost, \
performance, complexity, hardware needs, or what becomes possible later. Different \
ways to do the SAME thing with no real consequence are NOT a decision.

Set has_options=true when there IS such a branch. This covers TWO cases:
1. An explicit comparison the research raises ("firewall" → OPNsense vs pfSense vs \
a Linux box; "store time-series data" → Postgres+Timescale vs InfluxDB vs \
Prometheus).
2. A consequential decision the GOAL forces even if the research reads as \
step-by-step how-to. When the goal is to BUILD or SET UP something, the operator \
often faces key choices the plan should not silently pick — e.g. a home lab: how to \
lay out storage (ZFS vs LVM-thin vs a single disk — differ on redundancy, RAM, \
snapshots), where backups go (on-host vzdump vs a NAS vs offsite/cloud — differ on \
disaster recovery), how media transcoding is handled (CPU vs GPU passthrough). \
Surface the SINGLE most consequential such decision, using the concrete approaches \
the research actually describes as the options.

Set has_options=FALSE when there is no consequential choice: a purely \
factual/single-answer topic ("what port does postgres use", "how does TLS work"); \
a task with one obvious path; OR when the 'alternatives' are interchangeable ways \
to do the same thing with no real consequence — which library function to call \
(os.rename vs pathlib.rename), trivial style choices, or steps that simply all have \
to be done. A decision CHANGES the outcome, not just the syntax. Do NOT invent \
choices to fill the field. When torn, choose false.

When true: name the ONE core decision, then 2-4 options grounded in the research \
(real tools/approaches it mentions). For each: a short label, who/what it best FITS \
(tailored to the "User's goal / needs" when given, else to what the topic implies), \
and its main TRADE-OFF. Then suggest which you'd lean toward — it MUST be one of the \
options you listed — and the ONE main reason, framed as a suggestion the user is \
free to reject ("I'd lean X because Y — but it's your call"). Base the options on \
the research provided; don't add outside facts. Call surface_options exactly once."""

SURFACE_OPTIONS_TOOL = Tool(
    name="surface_options",
    description=(
        "Report whether the researched topic presents the user with a real "
        "decision between distinct viable approaches, and if so lay out the "
        "options tailored to their needs. has_options=false for a straightforward "
        "factual/single-answer topic — do not fabricate choices."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "has_options": {
                "type": "boolean",
                "description": "true ONLY if 2+ meaningfully-distinct viable paths exist.",
            },
            "decision": {
                "type": "string",
                "description": "The ONE core choice the user faces (empty if has_options=false).",
            },
            "options": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "label": {"type": "string", "description": "Short name for the approach"},
                        "fit": {"type": "string", "description": "Who/what it best suits"},
                        "tradeoff": {"type": "string", "description": "Its main downside/cost"},
                    },
                    "required": ["label", "fit", "tradeoff"],
                },
            },
            "suggested": {
                "type": "string",
                "description": "The label of the option you'd lean toward (must match one above).",
            },
            "why": {"type": "string", "description": "One-line reason for the suggestion."},
        },
        "required": ["has_options"],
    },
)


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
                "description": "5-12 distinct facets covering different aspects/sub-topics of the topic",
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

def _recency_directive() -> str:
    """§17.549 — soft recency bias at the SYNTHESIS stage (what the model
    reliably applies): tells it today's date so decompose/extract favor the
    most up-to-date information. Query-side recency is handled deterministically
    by ``_apply_recency_cue`` (the model proved unreliable at adding cues to
    queries). No SearXNG ``time_range`` — older sources aren't hard-filtered."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return (
        f"Today's date is {today}. Prioritize the most up-to-date information: "
        f"prefer current stable versions, recent releases, and recent sources; "
        f"when sources conflict or duplicate, favor the newer one. Treat older "
        f"information as potentially outdated unless it is foundational."
    )


def _sys(prompt: str) -> str:
    """Prepend the §17.549 recency directive to a system prompt."""
    return f"{_recency_directive()}\n\n{prompt}"


_YEAR_RE = re.compile(r"\b(?:19|20)\d{2}\b")


def _apply_recency_cue(query_text: str) -> str:
    """§17.549 — soft, deterministic recency: append the current year to a
    search query that doesn't already name a year, biasing SearXNG toward fresh
    results without a hard ``time_range`` filter. Gated on
    ``settings.research_recency_query_boost``."""
    if not query_text or not settings.research_recency_query_boost:
        return query_text
    if _YEAR_RE.search(query_text):
        return query_text
    return f"{query_text} {datetime.now(timezone.utc).strftime('%Y')}"


async def _decompose_topic(
    topic: str,
    *,
    role: str = "model_verifier",
    overrides: dict | None = None,
    existing_facets: list | None = None,
    gap_focus: str | None = None,
    session_id: str | None = None,
) -> dict:
    """Decompose topic into queries. Retries once on <2 facets; falls back.

    §17.89 Pattern 3 — dispatch via role= so MODEL_VERIFIER_PROVIDER is honored.
    """
    prompt = f"Decompose this research topic into search queries:\n\nTOPIC: {topic}"
    if existing_facets:
        prompt += f"\n\nAlready covered facets (do NOT repeat): {', '.join(existing_facets)}"
    if gap_focus:
        prompt += f"\n\nFocus specifically on these gaps: {gap_focus}"

    resp = await _bounded_tool_call(
        messages=[
            {"role": "system", "content": _sys(DECOMPOSE_SYSTEM_V1)},
            {"role": "user", "content": prompt},
        ],
        tools=[PLAN_RESEARCH_TOOL],
        role=role, overrides=overrides, temperature=0.4, max_tokens=2048,
        session_id=session_id,
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
        retry_resp = await _bounded_tool_call(
            messages=[
                {"role": "system", "content": _sys(DECOMPOSE_SYSTEM_V1)},
                {"role": "user", "content": retry_prompt},
            ],
            tools=[PLAN_RESEARCH_TOOL],
            role=role, overrides=overrides, temperature=0.5, max_tokens=2048,
            session_id=session_id,
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

    client = get_searxng_client()

    # §17.543 — Phase 1: dedup queries + resolve cache hits sequentially (cheap
    # Redis gets). Each result group is (facet, [result dicts]); facet travels
    # with its results so the concurrent fan-out below stays order-independent.
    cache_groups: list[tuple[str, list[dict]]] = []
    pending: list[dict] = []
    seen_in_batch: set[str] = set()
    for q in queries[:settings.research_max_queries]:
        # §17.549 — soft recency: bias the query toward fresh results, and store
        # it back on q so the Phase-2 fetch + cache key use the same text.
        q["query"] = _apply_recency_cue((q["query"] or "").strip())
        query_text = q["query"]
        query_key = query_text.lower()
        if not query_key or query_key in state.search_history or query_key in seen_in_batch:
            continue
        seen_in_batch.add(query_key)
        cached = await _searxng_cache_get(query_text)
        if cached is not None:
            logger.info("searxng_cache_hit: query=%s results=%d", query_text, len(cached))
            state.search_history.add(query_key)
            cache_groups.append((q.get("facet", ""), cached))
        else:
            pending.append(q)

    # §17.543 — Phase 2: fetch cache-MISS queries concurrently, bounded by a
    # semaphore. The per-query delay is held INSIDE the slot as a cooldown, so
    # the effective request rate stays ~concurrency/delay — politeness to the
    # upstream engines SearXNG fans out to, just no longer a strict serial wait.
    sem = asyncio.Semaphore(settings.research_searxng_concurrency)

    async def _fetch_one(q: dict) -> tuple[str, list[dict]]:
        query_text = (q["query"] or "").strip()
        query_key = query_text.lower()
        async with sem:
            try:
                # §17.503 — send ONLY `engines`, NOT `categories`. SearXNG treats
                # the two as ADDITIVE: passing `categories=it` activates *every*
                # engine tagged `it` (including MDN, which keyword-matches
                # aggressively) regardless of the curated `engines` list, so a
                # clean homelab query flooded with developer.mozilla.org pages.
                # Engines-only makes the curated list authoritative.
                resp = await client.get(
                    "/search",
                    params={
                        "q": query_text,
                        "format": "json",
                        "engines": _engines_for_category(q.get("search_category", "general")),
                    },
                )
                if resp.status_code == 200:
                    results = resp.json().get("results", [])[:10]
                    # §17.712 — 0-results fallback. The category engines returned
                    # nothing (commonly a transient CAPTCHA/rate-limit on the
                    # general engines). Retry ONCE with the widest general net so
                    # a single blocked engine can't zero the query. Only on empty,
                    # so it costs nothing on the common path.
                    if not results:
                        try:
                            fb = await client.get(
                                "/search",
                                params={"q": query_text, "format": "json",
                                        "engines": SEARXNG_FALLBACK_ENGINES},
                            )
                            if fb.status_code == 200:
                                results = fb.json().get("results", [])[:10]
                                if results:
                                    logger.info(
                                        "searxng_fallback_recovered: query=%s results=%d",
                                        query_text, len(results),
                                    )
                        except Exception as e:
                            logger.warning("searxng_fallback_failed: query='%s' error=%s",
                                           query_text, e)
                    await _searxng_cache_set(query_text, results)
                    logger.info("searxng_cache_miss: query=%s results=%d", query_text, len(results))
                    state.search_history.add(query_key)
                    return q.get("facet", ""), results
                # Non-200: mark attempted (matches pre-§17.543 behavior) but no results.
                state.search_history.add(query_key)
            except Exception as e:
                # Exception leaves query_key un-added so a later iteration may retry.
                logger.warning("research_search_failed: query='%s' error=%s", query_text, e)
            finally:
                await asyncio.sleep(settings.research_searxng_delay)
        return q.get("facet", ""), []

    miss_groups = await asyncio.gather(*[_fetch_one(q) for q in pending]) if pending else []

    # §17.543 — Phase 3: dedup URLs sequentially across all groups. Done after
    # the gather (single coroutine) so url_history stays race-free and the
    # winning duplicate is deterministic (cache hits first, then misses).
    all_results: list[dict] = []
    for facet, results in [*cache_groups, *miss_groups]:
        for r in results:
            url = r.get("url", "")
            if url and url not in state.url_history:
                state.url_history.add(url)
                all_results.append({
                    "title": r.get("title", ""),
                    "url": url,
                    "content": r.get("content", ""),
                    "facet": facet,
                })

    # §17.802 — depth-scaled cap: shallow stays lean, deeper runs get more breadth.
    return all_results[:settings.research_max_urls_for_depth(state.depth)]


async def _extract_entries(
    results: list[dict],
    topic: str,
    *,
    role: str = "model_research_extract",
    overrides: dict | None = None,
    session_id: str | None = None,
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
        # §17.291 — `total_urls` is the denominator the operator needs:
        # `bypassed_urls=5` is ambiguous without it (5/5 = broken
        # classifier eligible-everything, vs 5/100 = 5% bypass rate
        # which is normal). `bypassed_entries` is a per-chunk count
        # from the bypass path only; the corresponding total isn't
        # knowable here because the distill loop below hasn't run yet
        # — operators read it in the context of the bypass path.
        logger.info(
            "topic_classifier_bypass: total_urls=%d bypassed_urls=%d "
            "bypassed_entries=%d distill_urls=%d",
            len(results), bypass_url_count, bypass_entry_count,
            len({r.get("url") for r in expanded_results if r.get("url")}),
        )

    batch_size = _EXTRACT_BATCH_FULL_PAGE if fetched else _EXTRACT_BATCH_SNIPPET

    for i in range(0, len(expanded_results), batch_size):
        batch = expanded_results[i:i + batch_size]
        results_text = "\n\n".join(
            f"Title: {r['title']}\nURL: {r['url']}\nSnippet: {r['content'][:600]}"
            for r in batch
        )
        resp = await _bounded_tool_call(
            messages=[
                {"role": "system", "content": _sys(EXTRACT_SYSTEM_V1)},
                {"role": "user", "content": EXTRACT_PROMPT_V1.format(topic=topic, results=results_text)},
            ],
            tools=[RECORD_ENTRIES_TOOL],
            role=role, overrides=overrides, temperature=0.1, max_tokens=4096,
            session_id=session_id,
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
        elif resp and resp.success:
            # The LLM call succeeded but returned no parseable `entries`
            # tool-call — a tool-calling miss, NOT a transport/LLM failure.
            # Mislabeling this as extraction_llm_failed (with success=True)
            # obscured a real signal: a high rate here means the extractor
            # model is dropping to the non-LLM fallback. (§17.546)
            logger.warning(
                "extraction_no_tool_args: batch=%d (LLM responded but emitted "
                "no parseable `entries` tool-call; using non-LLM fallback)",
                i // batch_size + 1,
            )
        else:
            logger.warning(
                "extraction_llm_failed: batch=%d success=%s error=%s",
                i // batch_size + 1,
                resp.success if resp else False,
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
                        # §17.564 — provenance on the topic extraction-fallback
                        # path (the LLM-success path above already sets it).
                        "provenance": build_provenance(source_ref=r.get("url", "")),
                    })
                    logger.info("extraction_fallback: url='%s'", r.get("url", ""))

    return all_entries


async def _analyze_gaps(
    state: ResearchState,
    *,
    role: str = "model_verifier",
    overrides: dict | None = None,
    session_id: str | None = None,
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
        resp = await _bounded_tool_call(
            messages=[
                {"role": "system", "content": _sys(GAP_SYSTEM_V1)},
                {"role": "user", "content": prompt},
            ],
            tools=[ASSESS_COVERAGE_TOOL],
            role=role, overrides=overrides, temperature=0.3, max_tokens=2048,
            session_id=session_id,
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


# §17.169 — per-LLM-call timeout for the research path. Each individual
# model_router.tool_call inside _extract_entries / _analyze_gaps /
# _decompose_topic / URL-mode + PDF-mode extract has model_router's
# 30-min local_timeout HTTP ceiling but no shorter client-side bound.
# A single hung call stalled the whole iteration for the full 30 min
# (§17.166 caught the summary half; this catches everything else).
# 300 s (5 min) is 5× the summary cap and well under the HTTP ceiling.
# On timeout the helpers return a synthetic failed ModelResponse so the
# callers' existing fallback paths (chunk-based ingest, retry, etc.)
# fire automatically without per-call-site special-casing.
_RESEARCH_LLM_TIMEOUT_S = 300


async def _bounded_tool_call(
    *, session_id: str | None = None, **kwargs,
) -> ModelResponse:
    """§17.169 — timeout-bounded wrapper around model_router.tool_call.

    Returns a synthetic failed ModelResponse on timeout so callers'
    existing success=False fallback branches handle it cleanly.

    §17.208 — when ``session_id`` is provided and the underlying call
    returns ``success=True``, tickles ``research_sessions.last_activity_at``
    so the §17.85 reaper sees per-LLM-call forward progress during
    multi-batch topic-mode iterations. Gated on ``resp.success`` so the
    §17.169 synthetic-failure response does NOT count as progress —
    preserves §17.167's "wedged calls let the reaper kill the session"
    invariant. The touch is fail-soft inside ``_touch_last_activity``.
    """
    try:
        resp = await asyncio.wait_for(
            model_router.tool_call(**kwargs),
            timeout=_RESEARCH_LLM_TIMEOUT_S,
        )
        if session_id is not None and resp.success:
            await _touch_last_activity(session_id)
        return resp
    except asyncio.TimeoutError:
        logger.warning(
            "research_llm_timeout: kind=tool_call role=%s timeout_s=%d",
            kwargs.get("role") or kwargs.get("model"),
            _RESEARCH_LLM_TIMEOUT_S,
        )
        return ModelResponse(
            model="<timeout>",
            success=False,
            error=f"research_llm_timeout after {_RESEARCH_LLM_TIMEOUT_S}s",
            provider="<timeout>",
        )


# §17.166 — summary prompt budget. Pre-fix the prompt concatenated up
# to 60 entries verbatim, which on content-heavy Wikipedia pages
# (Software_design_pattern, etc.) easily blew past qwen2.5:7b's 4K
# context. Ollama's behavior on context overflow can be to hang
# indefinitely rather than error cleanly; combined with the
# 30-min local_timeout this turned every overflowed summary into a
# 30-min stall, blocking subsequent ingests via the single-running-
# session guard. Hard cap at ~6 KB total prompt body keeps us well
# under context with overhead for the system prompt + topic header.
_SUMMARY_PROMPT_BUDGET_CHARS = 6000
# Per-call timeout (seconds). qwen2.5:7b on this host produces ~2 KB
# summaries from a 6 KB prompt in 15-45 s. 120 s gives 2-8× margin
# without letting a wedged Ollama hold the session running for the
# full 30-min HTTP local_timeout.
_SUMMARY_PROMPT_TIMEOUT_S = 120
# §17.559 — output budget for the summary generation. Was 2048; the cloud
# default model_verifier (qwen3.5:397b-cloud) is a THINKING model that spends
# num_predict on reasoning before emitting content, so 2048 returned
# success=True + EMPTY text on some topics (score_research.py §17.558 caught
# `analog-filters` empty 2/2 runs). 8192 gives the reasoning room to still
# leave a real summary; paired with retry-on-empty below. See the
# "thinking model empty content" issue.
_SUMMARY_MAX_TOKENS = 8192


# §17.445 (Phase A / A2) — research summaries were an UN-ATTRIBUTED synthesis:
# per-entry source URLs live in state.all_entries but were stripped before the
# summarizer and absent from the complete payload. These helpers surface them as
# post-hoc citation (the SOTA-preferred attribution for synthesis tasks) without
# touching summary generation.
_MAX_SOURCES_RENDERED = 15


def _build_sources_list(state: "ResearchState") -> list[dict]:
    """Deduped, confidence-ranked source list from collected entries.

    Returns ``[{"url", "source_type", "confidence_score"}]`` best-first; one
    row per distinct URL (keeps the highest-confidence occurrence).
    """
    by_url: dict[str, dict] = {}
    for e in state.all_entries:
        url = (e.get("source") or "").strip()
        if not url:
            continue
        conf = e.get("confidence_score")
        conf = float(conf) if isinstance(conf, (int, float)) else 0.0
        existing = by_url.get(url)
        if existing is None or conf > existing["confidence_score"]:
            by_url[url] = {
                "url": url,
                "source_type": e.get("source_type") or "unknown",
                "confidence_score": round(conf, 2),
            }
    return sorted(
        by_url.values(), key=lambda s: s["confidence_score"], reverse=True
    )


def _attach_sources_block(summary: str, state: "ResearchState") -> str:
    """Append a deterministic ``**Sources**`` markdown block to a summary."""
    srcs = _build_sources_list(state)
    if not srcs:
        return summary
    shown = srcs[:_MAX_SOURCES_RENDERED]
    lines = "\n".join(
        f"- {s['url']} ({s['source_type']}, confidence {s['confidence_score']:.2f})"
        for s in shown
    )
    more = (
        f"\n…and {len(srcs) - len(shown)} more."
        if len(srcs) > len(shown) else ""
    )
    return f"{summary}\n\n**Sources** ({len(srcs)}):\n{lines}{more}"


# §17.799 — cite-aware summary. When citation_faithfulness_check_enabled is on,
# the summary is generated over NUMBERED sources with a prompt that asks for
# inline [n] markers, then scored per-citation (does source [n] actually support
# the sentence it's attached to?). Default path (flag off) is byte-unchanged.
# Bounded so the numbered body fits the summary prompt budget and the model can
# realistically cite each source.
_CITE_SUMMARY_MAX_SOURCES = 10
_CITE_SUMMARY_SRC_CHARS = 500
_CITE_SUMMARY_SYSTEM = (
    "You are a research summarizer. Write a concise, well-structured summary of "
    "the NUMBERED sources provided. After each sentence, cite the source "
    "number(s) that support it in square brackets, e.g. 'Vectors are normalized "
    "[2].' Cite ONLY sources that actually support the sentence — never invent a "
    "citation or cite a number not in the list. Base every statement on the "
    "sources; do not add outside knowledge."
)


def _build_numbered_summary_sources(state: "ResearchState") -> list[dict]:
    """Ordered, confidence-ranked, deduped-by-URL sources WITH content, for
    cite-aware summary generation + scoring. The list index defines the ``[n]``
    numbering (source ``[1]`` == ``result[0]``); ``text`` is the citeable content.
    """
    by_url: dict[str, dict] = {}
    for e in state.all_entries:
        url = (e.get("source") or "").strip()
        content = (e.get("content") or "").strip()
        if not url or not content:
            continue
        conf = e.get("confidence_score")
        conf = float(conf) if isinstance(conf, (int, float)) else 0.0
        existing = by_url.get(url)
        if existing is None or conf > existing["confidence_score"]:
            by_url[url] = {
                "url": url,
                "source_type": e.get("source_type") or "unknown",
                "confidence_score": round(conf, 2),
                "text": content[:_CITE_SUMMARY_SRC_CHARS],
            }
    ranked = sorted(by_url.values(), key=lambda s: s["confidence_score"], reverse=True)
    return ranked[:_CITE_SUMMARY_MAX_SOURCES]


def _build_cite_summary_prompt_body(sources: list[dict]) -> str:
    """Render numbered sources as ``[1] <text>`` blocks under the char budget."""
    out: list[str] = []
    used = 0
    for i, s in enumerate(sources, start=1):
        line = f"[{i}] {s['text']}"
        if used + len(line) + 2 > _SUMMARY_PROMPT_BUDGET_CHARS:
            break
        out.append(line)
        used += len(line) + 2
    return "\n\n".join(out)


async def _maybe_score_citation_faithfulness(
    summary_text: str, sources: list[dict], overrides: dict | None,
) -> dict | None:
    """§17.799 — gate + run the per-citation attribution check on the cite-aware
    summary. Default-off, fail-soft (→ None on disable/no-sources/error)."""
    if not settings.citation_faithfulness_check_enabled or not sources:
        return None
    try:
        from app.modules.citation_faithfulness import score_citation_faithfulness
        return await score_citation_faithfulness(
            summary_text,
            [s.get("text", "") for s in sources],  # 1-indexed to match the [n] prompt
            role=settings.citation_faithfulness_model_role,
            overrides=overrides,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("citation_faithfulness_wire_error: %s", exc)
        return None


async def _maybe_score_faithfulness(
    summary_text: str, state: "ResearchState", overrides: dict | None,
) -> dict | None:
    """§17.448 (B1) — gate + run the faithfulness check. Default-off, fail-soft."""
    if not settings.faithfulness_check_enabled or not state.all_entries:
        return None
    try:
        from app.modules.faithfulness import score_faithfulness
        return await score_faithfulness(
            summary_text,
            _build_summary_prompt_body(state),  # the same source content the summary was built from
            role=settings.faithfulness_model_role,
            overrides=overrides,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("faithfulness_wire_error: %s", exc)
        return None


async def _maybe_cove_revise(
    summary_text: str, state: "ResearchState", overrides: dict | None,
) -> str:
    """§17.452 (CoVe) — gate + run the revision pass. Default-off, fail-soft;
    returns the revised summary, or the original unchanged on disable/error."""
    if not settings.cove_check_enabled or not state.all_entries:
        return summary_text
    try:
        from app.modules.cove import cove_revise
        result = await cove_revise(
            summary_text,
            _build_summary_prompt_body(state),
            role=settings.cove_model_role,
            overrides=overrides,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("cove_wire_error: %s", exc)
        return summary_text
    if result and result.get("revised"):
        state.cove = {"changed": bool(result.get("changed")),
                      "questions": len(result.get("questions") or [])}
        return result["revised"]
    return summary_text


def _finalize_summary_text(summary_text: str, state: "ResearchState") -> str:
    """Attach the Sources block (A2) + CoVe (C) + faithfulness (B1) notes."""
    out = _attach_sources_block(summary_text, state)
    c = getattr(state, "cove", None)
    if c and c.get("changed"):
        out += (
            f"\n\n_Chain-of-Verification: revised after {c['questions']} "
            "verification checks against the sources._"
        )
    f = getattr(state, "faithfulness", None)
    if f:
        out += (
            f"\n\n_Faithfulness: {f['score']:.2f} — "
            f"{f['supported']}/{f['total']} summary claims grounded in the collected sources._"
        )
    cf = getattr(state, "citation_faithfulness", None)
    if cf:
        out += (
            f"\n\n_Citation faithfulness: {cf['score']:.2f} — "
            f"{cf['supported']}/{cf['total']} inline citations supported by their cited source"
            + (f" ({cf['dangling']} dangling)" if cf.get("dangling") else "")
            + "._"
        )
    return out


def _build_summary_prompt_body(state: "ResearchState") -> str:
    """Pack as many ``[facet] content`` lines as fit under the char budget.

    Trims by char count (a coarse-but-safe proxy for token count) instead
    of by entry count. A 60-entry cap on content-rich pages overflows the
    model context; this version stops adding entries when the running body
    would push past the budget. Returns the concatenated body (without
    the topic / entry-count header — caller prepends those).
    """
    out: list[str] = []
    used = 0
    for e in state.all_entries:
        line = f"[{e.get('facet', '?')}] {e.get('content', '')}"
        if used + len(line) + 1 > _SUMMARY_PROMPT_BUDGET_CHARS:
            break
        out.append(line)
        used += len(line) + 1
    return "\n".join(out)


async def _generate_options(
    state: ResearchState,
    summary_text: str,
    *,
    overrides: dict | None = None,
    context: str | None = None,
) -> dict | None:
    """§17.662 — surface user-tailored decision options from the research, but
    ONLY when the topic is decision-shaped. Returns a normalized dict
    ``{decision, options:[{label,fit,tradeoff}], suggested, why}`` or ``None``
    (not applicable / disabled / error). Fail-soft: never raises, never blocks
    finalize. Requires ≥2 distinct options — a single 'choice' is not a branch.

    §17.664 — ``context`` (the user's goal/brief, when known) is threaded into
    the prompt so the ``fit`` lines and the suggestion are tailored to *their*
    needs, not just the bare topic. The DAG-job path (§17.663) passes the brief
    description here."""
    if not settings.research_options_enabled:
        return None
    role = settings.research_options_model_role or "model_general"
    facets = ", ".join(
        sorted({str(e.get("facet", "")) for e in state.all_entries if e.get("facet")})[:12]
    )
    ctx_line = ""
    if context and context.strip():
        ctx_line = f"User's goal / needs: {context.strip()[:600]}\n"
    prompt = (
        f"Topic: {state.topic}\n"
        f"{ctx_line}"
        f"Facets covered: {facets}\n\n"
        f"Research summary:\n{summary_text[:4000]}\n\n"
        "Call surface_options."
    )
    try:
        resp = await _bounded_tool_call(
            messages=[
                {"role": "system", "content": _sys(OPTIONS_SYSTEM_V1)},
                {"role": "user", "content": prompt},
            ],
            tools=[SURFACE_OPTIONS_TOOL],
            role=role, overrides=overrides, temperature=0.3, max_tokens=1536,
        )
    except Exception as exc:  # never block finalize on the options step
        logger.warning("research_options_failed: topic=%s err=%s", state.topic, exc)
        return None
    parsed = read_tool_args(resp)
    if not parsed or not parsed.get("has_options"):
        return None
    opts = [
        {
            "label": str(o.get("label", "")).strip(),
            "fit": str(o.get("fit", "")).strip(),
            "tradeoff": str(o.get("tradeoff", "")).strip(),
        }
        for o in (parsed.get("options") or [])
        if isinstance(o, dict) and str(o.get("label", "")).strip()
    ][: max(2, int(settings.research_options_max))]
    if len(opts) < 2:  # a real branch needs at least two distinct paths
        return None
    # §17.664 — a suggestion must name one of the listed options; otherwise the
    # "I'd lean X" render would point at an option that isn't shown. Drop it.
    labels = {o["label"] for o in opts}
    suggested = str(parsed.get("suggested", "")).strip()
    if suggested not in labels:
        suggested = ""
    return {
        "decision": str(parsed.get("decision", "")).strip(),
        "options": opts,
        "suggested": suggested,
        "why": str(parsed.get("why", "")).strip() if suggested else "",
    }


def _render_options_block(data: dict) -> str:
    """§17.662 — the '🔀 Your options' section appended to a research summary."""
    decision = (data.get("decision") or "").strip() or "Which approach fits you best?"
    lines = ["", "---", "", f"### 🔀 Your options — {decision}", ""]
    for o in data.get("options") or []:
        lines.append(
            f"- **{o.get('label', '')}** — fits: {o.get('fit', '')} "
            f"_Trade-off:_ {o.get('tradeoff', '')}"
        )
    suggested = (data.get("suggested") or "").strip()
    if suggested:
        why = (data.get("why") or "").strip()
        tail = f" because {why}" if why else ""
        lines += ["", f"_I'd lean **{suggested}**{tail} — but it's your call._"]
    return "\n".join(lines)


async def _generate_summary(
    state: ResearchState,
    *,
    role: str = "model_verifier",
    overrides: dict | None = None,
) -> str:
    """Generate human-readable summary of all collected research.

    §17.89 Pattern 3 — dispatch via role= so MODEL_VERIFIER_PROVIDER is honored.
    §17.166 — char-budgeted prompt + per-call timeout. The fallback string
    is the same shape produced by the resp.success=False branch, so
    callers can't tell timeout from upstream failure (intentional —
    both produce a partial summary; the LLM call path always reaches
    finalize_session within bounded wall time).
    """
    # §17.799 — cite-aware mode: generate over NUMBERED sources with a prompt that
    # asks for inline [n] markers, so the per-citation check below has citations to
    # score. Falls back to the normal un-attributed summary if there are no usable
    # numbered sources. Flag off → default path, byte-unchanged.
    cite_sources: list[dict] = (
        _build_numbered_summary_sources(state)
        if settings.citation_faithfulness_check_enabled else []
    )
    cite_mode = bool(cite_sources)
    if cite_mode:
        system = _CITE_SUMMARY_SYSTEM
        prompt = (
            f"Summarize the research collected on: {state.topic}\n\n"
            f"Numbered sources ({len(cite_sources)}):\n\n"
            + _build_cite_summary_prompt_body(cite_sources)
        )
    else:
        system = SUMMARY_SYSTEM_V1
        prompt = (
            f"Summarize the research collected on: {state.topic}\n\n"
            f"Total entries: {len(state.all_entries)}\n\n"
            + _build_summary_prompt_body(state)
        )

    def _fallback() -> str:
        return _attach_sources_block(
            f"Research collected {len(state.all_entries)} entries on '{state.topic}'.",
            state,
        )

    # §17.559 — retry-on-empty. A thinking model can return success=True with
    # EMPTY content (budget spent on reasoning); one extra draw usually lands a
    # real summary. Timeout and genuine success=False fall back immediately (no
    # retry) — preserving the §17.166 fallback contract. Only the empty-success
    # case retries.
    summary_text = ""
    for attempt in range(2):
        try:
            resp = await asyncio.wait_for(
                model_router.generate(
                    prompt, role=role, overrides=overrides, system=system,
                    temperature=0.3, max_tokens=_SUMMARY_MAX_TOKENS,
                ),
                timeout=_SUMMARY_PROMPT_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "summary_timeout: topic=%s entries=%d budget_s=%d — falling back",
                state.topic, len(state.all_entries), _SUMMARY_PROMPT_TIMEOUT_S,
            )
            return _fallback()
        if not resp.success:
            return _fallback()
        summary_text = (resp.text or "").strip()
        if summary_text:
            break
        logger.warning(
            "summary_empty_content: topic=%s attempt=%d max_tokens=%d — %s",
            state.topic, attempt, _SUMMARY_MAX_TOKENS,
            "retrying" if attempt == 0 else "falling back",
        )

    if not summary_text:
        return _fallback()

    # §17.452 (Phase C / CoVe) — revise the summary against the sources FIRST,
    # so the faithfulness score below reflects the revised text (default-off).
    summary_text = await _maybe_cove_revise(summary_text, state, overrides)
    # §17.448 (Phase B / B1) — score the (possibly revised) summary against
    # the collected sources (default-off, fail-soft → None when disabled).
    state.faithfulness = await _maybe_score_faithfulness(
        summary_text, state, overrides,
    )
    # §17.799 — per-citation ATTRIBUTION score of the cite-aware summary against
    # the SPECIFIC source each [n] cites (default-off; None in the normal path
    # since it carries no citations). Scored on the delivered (post-CoVe) text.
    if cite_mode:
        state.citation_faithfulness = await _maybe_score_citation_faithfulness(
            summary_text, cite_sources, overrides,
        )
    finalized = _finalize_summary_text(summary_text, state)
    # §17.662 — branch out into user-tailored options when the topic is
    # decision-shaped (only-when-applicable → None for factual topics). Appended
    # AFTER the sources/notes so the factual summary stays clean above the choices.
    state.options = await _generate_options(state, summary_text, overrides=overrides)
    if state.options:
        finalized += "\n" + _render_options_block(state.options)
    return finalized


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
        # §17.445 (A2) — post-hoc source attribution for any consumer.
        "sources": _build_sources_list(state),
        # §17.448 (B1) — faithfulness of the summary vs sources (None if the
        # check is disabled or didn't run). Structured for programmatic readers.
        "faithfulness": getattr(state, "faithfulness", None),
        # §17.452 (CoVe) — whether the summary was revised by Chain-of-Verification.
        "cove": getattr(state, "cove", None),
        # §17.799 — per-citation attribution score (None unless the cite-aware
        # summary path ran). Structured for programmatic readers.
        "citation_faithfulness": getattr(state, "citation_faithfulness", None),
        # §17.662 — user-tailored decision options (None when the topic isn't
        # decision-shaped). Structured for programmatic readers; also rendered
        # into the summary text as a "🔀 Your options" block.
        "options": getattr(state, "options", None),
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
            _extract_entries(results, topic, overrides=overrides, session_id=session_id)
        )
        async for hb in _await_with_heartbeat(
            extract_task,
            {"status": "extracting", "iteration": state.iteration},
            session_id=session_id,
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

        gap_task = asyncio.create_task(_analyze_gaps(state, overrides=overrides, session_id=session_id))
        async for hb in _await_with_heartbeat(
            gap_task, {"status": "analyzing_gaps"},
            session_id=session_id,
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

        new_queries = gaps.get("gap_queries", [])
        if not new_queries:
            # §17.613 (audit #28) — distinguish a parse FAILURE from a genuine
            # no-gaps result. _analyze_gaps returns the gap_analysis_failed
            # sentinel (coverage=0, gap_queries=[]) whose docstring promises NOT
            # to terminate early; but `if not queries: break` did exactly that,
            # ending the run after one pass (common on the CPU verifier model).
            # On the sentinel, reuse this iteration's queries for one more pass
            # (max_iterations bounds the loop). A real no-gaps result still breaks.
            if gaps.get("reason") == "gap_analysis_failed":
                logger.info(
                    "gap_analysis_failed_retry: reusing queries for another pass "
                    "(iter=%s/%s)", state.iteration, state.max_iterations,
                )
                continue
            break
        queries = new_queries


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
            session_id=session_id,
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
            summary_task, {"status": "summarizing"},
            session_id=session_id,
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
# Direct modes: OpenAPI / GitHub / HF / Forum / URL / PDF
# =============================================================================
#
# §17.298 — the OpenAPI / GitHub / HF / Forum producer modes live in
# ``app/modules/research_modes/<mode>.py``. Each module exports a single
# ``run_research_<mode>_mode`` coroutine; we re-bind them here under
# their pre-§17.298 underscore-private aliases so the ``run_research``
# dispatch + every test that patches ``_run_research_*_mode`` keeps
# working byte-for-byte.
#
# URL + PDF modes stay inline below — they share the topic-loop's LLM
# extract path (``_extract_entries``, ``_unload_ollama_model``,
# ``_classify_extract_no_entries_reason``) which doesn't separate
# cleanly into a mode module. Future audit work can lift them
# separately if it becomes valuable.

from app.modules.research_modes import (  # noqa: E402
    forum as _forum_mode,
    github as _github_mode,
    hf as _hf_mode,
    openapi as _openapi_mode,
)

_run_research_openapi_mode = _openapi_mode.run_research_openapi_mode
_run_research_github_mode = _github_mode.run_research_github_mode
_run_research_hf_mode = _hf_mode.run_research_hf_mode
_run_research_forum_mode = _forum_mode.run_research_forum_mode



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
        # §17.209 — pass session_id to _bounded_tool_call (touch gated on
        # resp.success per §17.208); drop session_id from the outer heartbeat
        # so an all-timeout extract doesn't tickle last_activity_at via the
        # unconditional touch-on-task-completion path. A doomed session must
        # age out via the §17.85 reaper, not get spuriously kept alive.
        task = asyncio.create_task(_bounded_tool_call(
            messages=[
                {"role": "system", "content": _sys(EXTRACT_SYSTEM_V1)},
                {"role": "user", "content": EXTRACT_PROMPT_V1.format(topic=prompt_topic, results=results_text)},
            ],
            tools=[RECORD_ENTRIES_TOOL],
            role="model_research_extract",
            overrides=overrides,
            temperature=0.1,
            max_tokens=4096,
            session_id=session_id,
        ))
        async for hb in _await_with_heartbeat(
            task, {"status": "extracting", "iteration": 1},
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
                # §17.563 — attach provenance so ingest_entries writes a
                # rag_entry_provenance row (the distill path previously omitted
                # it, so distilled URL entries had no provenance/audit linkage).
                entry.setdefault("provenance", build_provenance(source_ref=src_url))
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
                        # §17.563 — provenance on the chunk-fallback path too.
                        "provenance": build_provenance(source_ref=url),
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
    from app.modules.provenance import build_provenance
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
        # §17.209 — same gating as URL-mode (§17.208 + comment at line 1840).
        task = asyncio.create_task(_bounded_tool_call(
            messages=[
                {"role": "system", "content": _sys(EXTRACT_SYSTEM_V1)},
                {"role": "user", "content": EXTRACT_PROMPT_V1.format(topic=filename, results=results_text)},
            ],
            tools=[RECORD_ENTRIES_TOOL],
            role="model_research_extract",
            overrides=overrides,
            temperature=0.1,
            max_tokens=4096,
            session_id=session_id,
        ))
        async for hb in _await_with_heartbeat(
            task, {"status": "extracting", "iteration": 1},
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
                # §17.564 — provenance so ingest_entries writes a
                # rag_entry_provenance row for PDF distill entries too.
                entry.setdefault(
                    "provenance", build_provenance(source_ref=virtual_url)
                )
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
                        # §17.564 — provenance on the PDF chunk-fallback path.
                        "provenance": build_provenance(source_ref=virtual_url),
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
    owner: str | None = None,
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
        topic, state_depth, research_domain, owner,
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

        decomposition = await _decompose_topic(
            topic, overrides=model_overrides, session_id=session_id,
        )
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
                session_id=session_id,
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
            session_id=session_id,
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
                session_id=session_id,
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
    owner: str | None = None,
) -> AsyncGenerator[str, None]:
    """Entry point for PDF research (called from /research/pdf endpoint).

    Lifecycle is delegated to ``_run_with_session_lifecycle`` so client
    disconnect finalizes the session as ``cancelled`` instead of orphaning
    it in ``running`` until the 30-min reaper.
    """
    t0 = time.monotonic()
    research_domain = domain or _detect_domain(filename)

    session_id, existing = await _guard_and_create_session(
        filename, "direct_pdf", research_domain, owner,
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


# ---------------------------------------------------------------------------
# §17.481 — background research kickoff (web launcher / fire-and-forget)
# ---------------------------------------------------------------------------

# Strong refs to in-flight background research tasks. asyncio.create_task only
# holds a weak ref, so without this the GC could collect a task mid-research
# and strand its session in 'running'. Mirrors §17.454's Phase-1 pattern.
_RESEARCH_BACKGROUND_TASKS: set[asyncio.Task] = set()


async def run_research_in_background(
    topic: str,
    depth: str = "medium",
    domain: str | None = None,
    model_overrides: dict | None = None,
) -> None:
    """§17.481 — drain ``run_research`` to completion off the request path so a
    web/CLI caller can fire-and-forget. ``run_research`` owns its session
    lifecycle (``_run_with_session_lifecycle`` finalizes the row on success,
    error, or cancellation), so we just consume the generator; any unexpected
    error is logged."""
    try:
        async for _ in run_research(
            topic=topic, depth=depth, domain=domain,
            model_overrides=model_overrides,
        ):
            pass
    except Exception:
        logger.exception("research_background_failed: topic=%s", (topic or "")[:80])


def spawn_research_background(
    topic: str,
    depth: str = "medium",
    domain: str | None = None,
    model_overrides: dict | None = None,
) -> asyncio.Task:
    """§17.481 — fire-and-forget background research with a strong ref so it
    survives GC, plus a done-callback to release the ref on completion."""
    task = asyncio.create_task(
        run_research_in_background(
            topic, depth=depth, domain=domain, model_overrides=model_overrides,
        )
    )
    _RESEARCH_BACKGROUND_TASKS.add(task)
    task.add_done_callback(_RESEARCH_BACKGROUND_TASKS.discard)
    return task
