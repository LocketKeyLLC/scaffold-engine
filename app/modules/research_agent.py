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
from urllib.parse import urlparse

from app.utils.llm_parsing import parse_json_object, parse_json_array
from app.database import async_session
from sqlalchemy import text
from app.modules.gt_extractor import _detect_topic_id

logger = logging.getLogger("scaffold.research")


# ---------------------------------------------------------------------------
# Domain auto-detection (reuses gt_extractor topic classifier)
# ---------------------------------------------------------------------------

TOPIC_TO_DOMAIN: dict[int, str] = {
    1: "llm",
    2: "rag",
    3: "eng",
    4: "eng",
    5: "eng",
    6: "eng",
}


def _detect_domain(topic: str) -> str:
    """Map a research topic to a Milvus partition domain via keyword scoring."""
    topic_id = _detect_topic_id(topic)
    return TOPIC_TO_DOMAIN.get(topic_id, "eng")


# ---------------------------------------------------------------------------
# SearXNG category -> engine routing
# ---------------------------------------------------------------------------
# SearXNG result cache (Redis, 1h TTL)
_SEARXNG_CACHE_TTL = 3600

def _searxng_cache_key(query: str) -> str:
    h = hashlib.sha256(query.encode("utf-8")).hexdigest()[:16]
    return f"searxng:{h}"

async def _searxng_cache_get(query: str):
    try:
        from app.utils.embedding_cache import get_cache
        r = await get_cache()._get_redis()
        raw = await r.get(_searxng_cache_key(query))
        if raw:
            return json.loads(raw)
    except Exception as e:
        logger.debug("searxng_cache_get_failed: query=%s error=%s", query, e)
    return None

async def _searxng_cache_set(query: str, results) -> None:
    try:
        from app.utils.embedding_cache import get_cache
        r = await get_cache()._get_redis()
        await r.setex(_searxng_cache_key(query), _SEARXNG_CACHE_TTL, json.dumps(results))
    except Exception as e:
        logger.debug("searxng_cache_set_failed: query=%s error=%s", query, e)

CATEGORY_ENGINES: dict[str, str] = {
    "it": "github,stackoverflow,pypi,google",
    "science": "arxiv,crossref,semantic_scholar,google",
    "news": "google news,bing news",
    "general": "google,bing,duckduckgo,brave",
}


def _engines_for_category(category: str) -> str:
    """Return a comma-separated engine string for a SearXNG search category."""
    return CATEGORY_ENGINES.get(category, "google,bing,duckduckgo")


# ---------------------------------------------------------------------------
# Source reliability scoring
# ---------------------------------------------------------------------------

DOMAIN_SCORES: dict[str, float] = {
    "arxiv.org": 0.95,
    "ieee.org": 0.95,
    "acm.org": 0.95,
    "docs.python.org": 0.90,
    "docs.microsoft.com": 0.90,
    "learn.microsoft.com": 0.90,
    "developer.mozilla.org": 0.90,
    "kubernetes.io": 0.90,
    "docs.docker.com": 0.90,
    "pytorch.org": 0.90,
    "huggingface.co": 0.90,
    "github.com": 0.80,
    "stackoverflow.com": 0.80,
    "wiki.archlinux.org": 0.80,
    "medium.com": 0.60,
    "dev.to": 0.60,
    "towardsdatascience.com": 0.60,
    "reddit.com": 0.50,
}

DEFAULT_SOURCE_SCORE: float = 0.50


def _score_source(url: str) -> float:
    """Return a reliability score for a URL based on its domain."""
    try:
        hostname = urlparse(url).hostname or ""
    except Exception:
        return DEFAULT_SOURCE_SCORE
    hostname = hostname.lower().removeprefix("www.")
    for domain_key, score in DOMAIN_SCORES.items():
        if domain_key in hostname:
            return score
    return DEFAULT_SOURCE_SCORE


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
    total_new: int = 0
    total_versioned: int = 0
    total_skipped_hash: int = 0
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
# Session tracking helpers
# ---------------------------------------------------------------------------

async def _guard_concurrent() -> dict | None:
    """Check for running research sessions. Returns existing row dict or None."""
    async with async_session() as db:
        row = await db.execute(
            text("SELECT id, topic FROM research_sessions WHERE status = 'running' LIMIT 1")
        )
        existing = row.mappings().first()
        return dict(existing) if existing else None


async def _create_session(topic: str, depth: str, domain: str) -> str:
    """Insert a new research_sessions row. Returns session UUID as string."""
    async with async_session() as db:
        result = await db.execute(
            text("""
                INSERT INTO research_sessions (topic, depth, domain, status)
                VALUES (:topic, :depth, :domain, 'running')
                RETURNING id
            """),
            {"topic": topic, "depth": depth, "domain": domain},
        )
        session_id = str(result.scalar_one())
        await db.commit()
        return session_id


async def _update_session_iteration(
    session_id: str,
    state: "ResearchState",
    coverage: float | None = None,
) -> None:
    """Update session counters after an iteration."""
    async with async_session() as db:
        await db.execute(
            text("""
                UPDATE research_sessions
                SET iterations_completed = :iters,
                    total_entries_extracted = :extracted,
                    total_entries_ingested = :ingested,
                    total_entries_rejected = :rejected,
                    total_urls_searched = :urls,
                    total_queries = :queries,
                    coverage_pct = COALESCE(:coverage, coverage_pct)
                WHERE id = :sid
            """),
            {
                "sid": session_id,
                "iters": state.iteration,
                "extracted": len(state.all_entries),
                "ingested": state.total_ingested,
                "rejected": state.total_rejected,
                "urls": len(state.url_history),
                "queries": len(state.search_history),
                "coverage": coverage,
            },
        )
        await db.commit()


async def _finalize_session(
    session_id: str,
    status: str,
    duration_ms: int,
    summary: str | None = None,
) -> None:
    """Mark session completed or failed."""
    async with async_session() as db:
        await db.execute(
            text("""
                UPDATE research_sessions
                SET status = :status,
                    completed_at = NOW(),
                    duration_ms = :dur,
                    summary = :summary
                WHERE id = :sid
            """),
            {"sid": session_id, "status": status, "dur": duration_ms, "summary": summary},
        )
        await db.commit()


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
        # Check Redis cache first
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
            state.search_history.add(query_text)
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


# ---------------------------------------------------------------------------
# Step 2.5: Full-page fetch + extraction via trafilatura
# ---------------------------------------------------------------------------

async def _fetch_and_extract(results: list[dict]) -> list[dict]:
    """Fetch URLs concurrently, extract clean text via trafilatura.

    Returns [{"url", "content"}] for pages where extracted text >= 100 chars.
    trafilatura.extract is synchronous/CPU-bound, so runs in a thread executor.
    One short-lived httpx.AsyncClient is reused across all URLs in the batch.
    """
    if not results:
        return []

    import httpx
    import trafilatura

    sem = asyncio.Semaphore(5)
    urls = [r["url"] for r in results if r.get("url")]

    async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
        async def _fetch_one(url: str) -> dict | None:
            async with sem:
                try:
                    resp = await client.get(url)
                    if resp.status_code != 200 or not resp.text:
                        return None
                    text = await asyncio.to_thread(
                        trafilatura.extract,
                        resp.text,
                        output_format="txt",
                        with_metadata=False,
                    )
                    if not text or len(text) < 100:
                        return None
                    return {"url": url, "content": text}
                except Exception as e:
                    logger.debug("trafilatura_fetch_failed: url=%s error=%s", url, e)
                    return None

        fetched = await asyncio.gather(*[_fetch_one(u) for u in urls])

    return [f for f in fetched if f is not None]


def _chunk_text(text: str, max_tokens: int = 1500, overlap_tokens: int = 200) -> list[str]:
    """Split text at paragraph boundaries, ~4 chars/token estimate, with overlap."""
    max_chars = max_tokens * 4
    overlap_chars = overlap_tokens * 4

    if len(text) <= max_chars:
        return [text]

    paragraphs = text.split("\n\n")
    chunks: list[str] = []
    current = ""

    for p in paragraphs:
        if len(current) + len(p) + 2 <= max_chars:
            current = f"{current}\n\n{p}" if current else p
        else:
            if current:
                chunks.append(current)
            if chunks and overlap_chars > 0:
                tail = chunks[-1][-overlap_chars:]
                current = f"{tail}\n\n{p}"
            else:
                current = p

    if current:
        chunks.append(current)

    return chunks


async def _check_contradictions(entries: list[dict]) -> list[dict]:
    """Scan entry pairs for potential conflicts via title word overlap.

    Heuristic: two entries whose titles share 2+ words (lowercased, whitespace-split)
    are flagged as candidates. Informational only — caller decides what to do.
    Capped at 5 pairs to avoid O(n^2) blowup on large batches.
    """
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


async def _extract_entries(
    results: list[dict],
    topic: str,
    model: str,
) -> list[dict]:
    """Distill search results into knowledge entries via LLM.

    Fetches full-page text via trafilatura where available; chunks long pages;
    falls back to SearXNG snippet when fetch/extract fails for a given URL.
    """
    if not results:
        return []

    # Full-page fetch; map url -> extracted text. Empty dict = total failure,
    # in which case we fall through to snippet-only behavior.
    fetched = await _fetch_and_extract(results)
    url_to_text: dict[str, str] = {f["url"]: f["content"] for f in fetched}
    if fetched:
        logger.info("research_fetch: %d/%d URLs extracted via trafilatura",
                    len(fetched), len(results))
    else:
        logger.warning("research_fetch: trafilatura returned nothing; snippet fallback")

    # Expand results: each becomes 1+ chunks. Missing full-page -> single snippet chunk.
    expanded: list[dict] = []
    for r in results:
        url = r.get("url", "")
        full = url_to_text.get(url)
        if full:
            for chunk in _chunk_text(full):
                expanded.append({
                    "title": r.get("title", ""),
                    "url": url,
                    "content": chunk,
                    "facet": r.get("facet", ""),
                })
        else:
            expanded.append(r)  # snippet fallback

    # Batch to stay within context limits. Full-page chunks are larger than
    # snippets, so reduce batch size when we have real content.
    batch_size = 5 if fetched else 10
    all_entries = []
    results = expanded

    for i in range(0, len(results), batch_size):
        batch = results[i:i + batch_size]
        entries = []
        results_text = "\n\n".join(
            f"Title: {r['title']}\nURL: {r['url']}\nSnippet: {r['content'][:600]}"
            for r in batch
        )

        resp = await model_router.generate(
            EXTRACT_PROMPT.format(topic=topic, results=results_text),
            model=model,
            system=EXTRACT_SYSTEM,
            temperature=0.1,
            max_tokens=1024,
        )

        if resp.success and resp.text and len(resp.text.strip()) > 5:
            entries = parse_json_array(resp.text) or []
            entries = [e for e in entries if isinstance(e, dict)]
            if entries:
                for entry in entries:
                    source_url = entry.get("source", "")
                    if source_url:
                        entry["confidence_score"] = _score_source(source_url)
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
                        "confidence_score": _score_source(r.get("url", "")),
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
    research_domain = domain or _detect_domain(topic)

    # ---- Concurrent research guard ----
    existing = await _guard_concurrent()
    if existing:
        yield _sse("error", {
            "message": f"Research already in progress: '{existing['topic']}'",
            "existing_session": str(existing["id"]),
            "http_status": 409,
        })
        return

    # ---- Create session row ----
    session_id = await _create_session(topic, depth, research_domain)

    state = ResearchState(
        topic=topic,
        depth=depth,
        domain=research_domain,
    )

    # URL-mode short-circuit: if topic is a URL, skip decompose/search
    if _is_url(topic):
        _extract_m = get_model("model_verifier", model_overrides)
        _summary_m = get_model("model_verifier", model_overrides)
        yield _sse("research_started", {
            "topic": topic,
            "depth": "direct_url",
            "domain": state.domain,
            "max_iterations": 1,
            "session_id": session_id,
            "mode": "direct_url",
        })
        async for _evt in _run_research_url_mode(
            url=topic,
            state=state,
            session_id=session_id,
            extract_model=_extract_m,
            summary_model=_summary_m,
            t0=t0,
        ):
            yield _evt
        return

    decompose_model = get_model("model_verifier", model_overrides)
    extract_model = get_model("model_verifier", model_overrides)
    summary_model = get_model("model_verifier", model_overrides)

    yield _sse("research_started", {
        "topic": topic,
        "depth": depth,
        "domain": state.domain,
        "max_iterations": state.max_iterations,
        "session_id": session_id,
    })

    try:
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
        coverage = None
        while state.iteration < state.max_iterations:
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

            # Contradiction check (informational — ingestion proceeds regardless)
            if entries:
                contradictions = await _check_contradictions(entries)
                if contradictions:
                    yield _sse("contradictions_detected", {
                        "count": len(contradictions),
                        "pairs": contradictions,
                    })
            # Ingest
            ingested = 0
            if entries:
                state.all_entries.extend(entries)
                stats = await ingest_entries(entries, domain=state.domain)
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

            # Update session after each iteration
            await _update_session_iteration(session_id, state, coverage)

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

        # ---- Final summary (with heartbeat) ----
        summary_task = asyncio.create_task(_generate_summary(state, model=summary_model))
        while not summary_task.done():
            await asyncio.sleep(8)
            if not summary_task.done():
                yield _sse("heartbeat", {"status": "summarizing"})
        summary = summary_task.result()
        elapsed_ms = int((time.monotonic() - t0) * 1000)

        # Finalize session — completed
        await _update_session_iteration(session_id, state, coverage)
        await _finalize_session(session_id, "completed", elapsed_ms, summary)

        yield _sse("research_complete", {
            "topic": topic,
            "session_id": session_id,
            "total_entries": len(state.all_entries),
            "total_ingested": state.total_ingested,
            "total_rejected": state.total_rejected,
            "new": state.total_new,
            "versioned": state.total_versioned,
            "rejected": state.total_rejected,
            "skipped_hash": state.total_skipped_hash,
            "iterations": state.iteration,
            "total_urls_searched": len(state.url_history),
            "total_queries": len(state.search_history),
            "duration_ms": elapsed_ms,
            "summary": summary,
            "domain": state.domain,
            "depth": depth,
        })

    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.error("research_failed: session=%s error=%s", session_id, exc, exc_info=True)
        await _finalize_session(session_id, "failed", elapsed_ms)
        yield _sse("error", {
            "message": f"Research failed: {exc}",
            "session_id": session_id,
            "topic": topic,
        })


# ---------------------------------------------------------------------------
# URL-mode: direct ingestion of a single URL (bypasses SearXNG discovery)
# ---------------------------------------------------------------------------

_MAX_URL_BYTES = 5 * 1024 * 1024  # 5 MB cap per page


def _is_url(s: str) -> bool:
    """True iff s is a valid absolute http(s) URL."""
    from urllib.parse import urlparse
    try:
        p = urlparse(s.strip())
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


async def _robots_allowed(url: str, user_agent: str = "ScaffoldEngine/1.0") -> bool:
    """Check robots.txt. Fail-open on any error (missing robots.txt = allowed)."""
    import httpx
    from urllib.parse import urlparse
    from urllib.robotparser import RobotFileParser
    try:
        p = urlparse(url)
        robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as c:
            r = await c.get(robots_url)
            if r.status_code >= 400:
                return True
            rp = RobotFileParser()
            rp.parse(r.text.splitlines())
            return rp.can_fetch(user_agent, url)
    except Exception as e:
        logger.debug("robots_check_failed: url=%s error=%s", url, e)
        return True


async def _fetch_url_bounded(url: str, max_bytes: int = _MAX_URL_BYTES) -> str | None:
    """Stream-fetch with hard byte cap. Returns text or None on failure/cap."""
    import httpx
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as c:
            async with c.stream("GET", url, headers={"User-Agent": "ScaffoldEngine/1.0"}) as resp:
                if resp.status_code != 200:
                    logger.warning("url_fetch_status: url=%s status=%d", url, resp.status_code)
                    return None
                cl = resp.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > max_bytes:
                    logger.warning("url_fetch_content_length_exceeded: url=%s bytes=%s", url, cl)
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > max_bytes:
                        logger.warning("url_fetch_cap_exceeded: url=%s bytes=%d", url, len(buf))
                        return None
                enc = resp.encoding or "utf-8"
                try:
                    return bytes(buf).decode(enc, errors="replace")
                except LookupError:
                    return bytes(buf).decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("url_fetch_failed: url=%s error=%s", url, e)
        return None


async def _run_research_url_mode(
    url: str,
    state: "ResearchState",
    session_id,
    extract_model: str,
    summary_model: str,
    t0: float,
):
    """URL-mode: fetch one URL, extract via trafilatura, ingest, summarize."""
    import trafilatura

    state.outline_facets = ["direct_url"]
    yield _sse("decomposition_complete", {
        "complexity": "direct",
        "facets": state.outline_facets,
        "query_count": 0,
    })

    state.iteration = 1
    yield _sse("iteration_started", {
        "iteration": 1,
        "query_count": 0,
        "mode": "direct_url",
    })

    # 1. Robots check
    if not await _robots_allowed(url):
        yield _sse("error", {
            "message": f"robots.txt disallows fetching {url}",
            "session_id": session_id,
            "topic": url,
        })
        await _finalize_session(session_id, "failed", int((time.monotonic() - t0) * 1000))
        return

    # 2. Bounded fetch
    html = await _fetch_url_bounded(url)
    if not html:
        yield _sse("error", {
            "message": f"Failed to fetch {url} (or exceeded 5MB cap)",
            "session_id": session_id,
            "topic": url,
        })
        await _finalize_session(session_id, "failed", int((time.monotonic() - t0) * 1000))
        return

    # 3. Trafilatura extract
    text = await asyncio.to_thread(
        trafilatura.extract, html, output_format="txt", with_metadata=False,
    )
    if not text or len(text) < 100:
        yield _sse("error", {
            "message": f"No extractable content at {url} (got {len(text or '')} chars)",
            "session_id": session_id,
            "topic": url,
        })
        await _finalize_session(session_id, "failed", int((time.monotonic() - t0) * 1000))
        return

    # Page title (best-effort)
    try:
        meta = await asyncio.to_thread(trafilatura.extract_metadata, html)
        page_title = (getattr(meta, "title", None) or url)[:200]
    except Exception:
        page_title = url[:200]

    yield _sse("search_complete", {
        "iteration": 1,
        "results_found": 1,
        "total_urls": 1,
        "mode": "direct_url",
    })

    state.url_history.add(url)
    state.search_history.add(f"direct:{url}")

    # 4. Chunk + LLM extract
    chunks = _chunk_text(text)
    prompt_topic = page_title if page_title and page_title != url[:200] else f"content at {url}"
    batch_size = 5
    all_entries: list[dict] = []

    for i in range(0, len(chunks), batch_size):
        batch_chunks = chunks[i:i + batch_size]
        results_text = "\n\n".join(
            f"Title: {page_title}\nURL: {url}\nSnippet: {c[:600]}"
            for c in batch_chunks
        )
        task = asyncio.create_task(model_router.generate(
            EXTRACT_PROMPT.format(topic=prompt_topic, results=results_text),
            model=extract_model,
            system=EXTRACT_SYSTEM,
            temperature=0.1,
            max_tokens=1024,
        ))
        while not task.done():
            await asyncio.sleep(8)
            if not task.done():
                yield _sse("heartbeat", {"status": "extracting", "iteration": 1})
        resp = task.result()

        entries = []
        if resp.success and resp.text and len(resp.text.strip()) > 5:
            entries = parse_json_array(resp.text) or []
            entries = [e for e in entries if isinstance(e, dict)]
            for entry in entries:
                src_url = entry.get("source", "") or url
                entry["source"] = src_url
                entry["confidence_score"] = _score_source(src_url)
                entry["facet"] = "direct_url"
            all_entries.extend(entries)
        else:
            logger.warning("url_mode_extract_failed: batch=%d success=%s error=%s",
                           i // batch_size, resp.success if resp else None,
                           resp.error if resp else "no-resp")

        # Fallback: one entry per chunk if LLM produced nothing
        if not entries:
            for c in batch_chunks:
                if len(c) > 50:
                    all_entries.append({
                        "title": page_title,
                        "content": c,
                        "tags": "",
                        "source": url,
                        "confidence_score": _score_source(url),
                        "source_type": "community",
                        "facet": "direct_url",
                    })

    yield _sse("extraction_complete", {
        "iteration": 1,
        "entries_extracted": len(all_entries),
    })

    # 5. Ingest
    ingested = 0
    if all_entries:
        state.all_entries.extend(all_entries)
        stats = await ingest_entries(all_entries, domain=state.domain)
        ingested = stats["new"] + stats["versioned"]
        state.total_new += stats["new"]
        state.total_versioned += stats["versioned"]
        state.total_rejected += stats["rejected"]
        state.total_skipped_hash += stats["skipped_hash"]
        state.total_ingested += ingested

    yield _sse("ingestion_complete", {
        "iteration": 1,
        "entries_ingested": ingested,
        "total_ingested": state.total_ingested,
        "total_rejected": state.total_rejected,
    })

    yield _sse("iteration_complete", {
        "iteration": 1,
        "entries_extracted": len(all_entries),
        "entries_ingested": ingested,
    })

    # 6. Summary + finalize
    summary_task = asyncio.create_task(_generate_summary(state, model=summary_model))
    while not summary_task.done():
        await asyncio.sleep(8)
        if not summary_task.done():
            yield _sse("heartbeat", {"status": "summarizing"})
    summary = summary_task.result()
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    await _update_session_iteration(session_id, state, 100)
    await _finalize_session(session_id, "completed", elapsed_ms, summary)

    yield _sse("research_complete", {
        "topic": url,
        "session_id": session_id,
        "total_entries": len(state.all_entries),
        "total_ingested": state.total_ingested,
        "total_rejected": state.total_rejected,
        "new": state.total_new,
        "versioned": state.total_versioned,
        "rejected": state.total_rejected,
        "skipped_hash": state.total_skipped_hash,
        "iterations": 1,
        "total_urls_searched": 1,
        "total_queries": 1,
        "duration_ms": elapsed_ms,
        "summary": summary,
        "domain": state.domain,
        "depth": "direct_url",
    })


# ---------------------------------------------------------------------------
# PDF-mode: direct ingestion of a single uploaded PDF (#4.5b)
# ---------------------------------------------------------------------------

_MAX_PDF_BYTES = 20 * 1024 * 1024  # 20 MB cap


def _extract_pypdf(pdf_bytes: bytes) -> tuple[str, int]:
    """Extract text via pypdf. Returns (text, page_count)."""
    import io
    from pypdf import PdfReader
    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = len(reader.pages)
    parts = []
    for page in reader.pages:
        try:
            t = page.extract_text() or ""
            if t.strip():
                parts.append(t)
        except Exception as e:
            logger.debug("pypdf_page_fail: error=%s", e)
    return ("\n\n".join(parts), pages)


def _extract_pdfplumber(pdf_bytes: bytes) -> tuple[str, int]:
    """Extract text via pdfplumber (better for structured/multi-column). Returns (text, page_count)."""
    import io
    import pdfplumber
    parts = []
    pages = 0
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = len(pdf.pages)
        for page in pdf.pages:
            try:
                t = page.extract_text() or ""
                if t.strip():
                    parts.append(t)
            except Exception as e:
                logger.debug("pdfplumber_page_fail: error=%s", e)
    return ("\n\n".join(parts), pages)


def _extract_threshold(page_count: int) -> int:
    """Minimum char count to consider extraction successful."""
    return max(200, page_count * 50)


async def _extract_pdf_text(
    pdf_bytes: bytes,
    extractor: str = "auto",
) -> tuple[str, int, str]:
    """Extract text from PDF bytes.

    extractor: 'auto' (pypdf → plumber fallback), 'pypdf' (force), 'plumber' (force).
    Returns (text, page_count, extractor_used).
    Raises RuntimeError on scanned/unreadable PDFs (both extractors return too little).
    """
    extractor = (extractor or "auto").lower()
    if extractor not in ("auto", "pypdf", "plumber"):
        extractor = "auto"

    # Force pypdf
    if extractor == "pypdf":
        text, pages = await asyncio.to_thread(_extract_pypdf, pdf_bytes)
        return (text, pages, "pypdf")

    # Force plumber
    if extractor == "plumber":
        text, pages = await asyncio.to_thread(_extract_pdfplumber, pdf_bytes)
        return (text, pages, "plumber")

    # Auto: try pypdf first
    text, pages = await asyncio.to_thread(_extract_pypdf, pdf_bytes)
    if len(text) >= _extract_threshold(pages):
        return (text, pages, "pypdf")

    # Fallback to plumber
    logger.info("pdf_extract_fallback: pypdf_chars=%d pages=%d threshold=%d",
                len(text), pages, _extract_threshold(pages))
    plumber_text, _ = await asyncio.to_thread(_extract_pdfplumber, pdf_bytes)
    if len(plumber_text) >= _extract_threshold(pages):
        return (plumber_text, pages, "plumber")

    # Both failed — likely scanned
    raise RuntimeError(
        f"PDF appears to be scanned or unreadable: "
        f"pypdf={len(text)} chars, plumber={len(plumber_text)} chars, pages={pages}"
    )


async def _run_research_pdf_mode(
    pdf_bytes: bytes,
    filename: str,
    extractor: str,
    state: "ResearchState",
    session_id,
    extract_model: str,
    summary_model: str,
    t0: float,
):
    """PDF-mode: extract text from uploaded bytes, ingest like URL-mode."""
    if len(pdf_bytes) > _MAX_PDF_BYTES:
        yield _sse("error", {
            "message": f"PDF exceeds {_MAX_PDF_BYTES // (1024*1024)}MB cap ({len(pdf_bytes)} bytes)",
            "session_id": session_id,
            "topic": filename,
        })
        await _finalize_session(session_id, "failed", int((time.monotonic() - t0) * 1000))
        return

    state.outline_facets = ["direct_pdf"]
    yield _sse("decomposition_complete", {
        "complexity": "direct",
        "facets": state.outline_facets,
        "query_count": 0,
    })

    state.iteration = 1
    yield _sse("iteration_started", {
        "iteration": 1,
        "query_count": 0,
        "mode": "direct_pdf",
    })

    # 1. Extract text (pypdf → plumber fallback by default)
    try:
        text, page_count, used = await _extract_pdf_text(pdf_bytes, extractor=extractor)
    except RuntimeError as e:
        yield _sse("error", {
            "message": str(e),
            "session_id": session_id,
            "topic": filename,
            "hint": "Scanned PDFs require OCR (not yet supported)",
        })
        await _finalize_session(session_id, "failed", int((time.monotonic() - t0) * 1000))
        return
    except Exception as e:
        yield _sse("error", {
            "message": f"PDF extraction failed: {e}",
            "session_id": session_id,
            "topic": filename,
        })
        await _finalize_session(session_id, "failed", int((time.monotonic() - t0) * 1000))
        return

    yield _sse("search_complete", {
        "iteration": 1,
        "results_found": 1,
        "total_urls": 1,
        "mode": "direct_pdf",
        "page_count": page_count,
        "extractor_used": used,
        "char_count": len(text),
    })

    virtual_url = f"pdf://{filename}"
    state.url_history.add(virtual_url)
    state.search_history.add(f"direct_pdf:{filename}")

    # 2. Chunk + LLM extract
    chunks = _chunk_text(text)
    batch_size = 5
    all_entries: list[dict] = []

    for i in range(0, len(chunks), batch_size):
        batch_chunks = chunks[i:i + batch_size]
        results_text = "\n\n".join(
            f"Title: {filename} (p. {page_count})\nURL: {virtual_url}\nSnippet: {c[:600]}"
            for c in batch_chunks
        )
        task = asyncio.create_task(model_router.generate(
            EXTRACT_PROMPT.format(topic=filename, results=results_text),
            model=extract_model,
            system=EXTRACT_SYSTEM,
            temperature=0.1,
            max_tokens=1024,
        ))
        while not task.done():
            await asyncio.sleep(8)
            if not task.done():
                yield _sse("heartbeat", {"status": "extracting", "iteration": 1})
        resp = task.result()

        entries = []
        if resp.success and resp.text and len(resp.text.strip()) > 5:
            entries = parse_json_array(resp.text) or []
            entries = [e for e in entries if isinstance(e, dict)]
            for entry in entries:
                entry["source"] = virtual_url
                entry["confidence_score"] = 0.8  # local upload, reasonably trusted
                entry["facet"] = "direct_pdf"
                entry["source_type"] = entry.get("source_type") or "tech_docs"
            all_entries.extend(entries)
        else:
            logger.warning("pdf_mode_extract_failed: batch=%d success=%s error=%s",
                           i // batch_size, resp.success if resp else None,
                           resp.error if resp else "no-resp")

        # Fallback: one entry per chunk if LLM produced nothing
        if not entries:
            for c in batch_chunks:
                if len(c) > 50:
                    all_entries.append({
                        "title": filename,
                        "content": c,
                        "tags": "",
                        "source": virtual_url,
                        "confidence_score": 0.8,
                        "source_type": "tech_docs",
                        "facet": "direct_pdf",
                    })

    yield _sse("extraction_complete", {
        "iteration": 1,
        "entries_extracted": len(all_entries),
    })

    # 3. Ingest
    ingested = 0
    if all_entries:
        state.all_entries.extend(all_entries)
        stats = await ingest_entries(all_entries, domain=state.domain)
        ingested = stats["new"] + stats["versioned"]
        state.total_new += stats["new"]
        state.total_versioned += stats["versioned"]
        state.total_rejected += stats["rejected"]
        state.total_skipped_hash += stats["skipped_hash"]
        state.total_ingested += ingested

    yield _sse("ingestion_complete", {
        "iteration": 1,
        "entries_ingested": ingested,
        "total_ingested": state.total_ingested,
        "total_rejected": state.total_rejected,
    })

    yield _sse("iteration_complete", {
        "iteration": 1,
        "entries_extracted": len(all_entries),
        "entries_ingested": ingested,
    })

    # 4. Summary + finalize
    summary_task = asyncio.create_task(_generate_summary(state, model=summary_model))
    while not summary_task.done():
        await asyncio.sleep(8)
        if not summary_task.done():
            yield _sse("heartbeat", {"status": "summarizing"})
    summary = summary_task.result()
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    await _update_session_iteration(session_id, state, 100)
    await _finalize_session(session_id, "completed", elapsed_ms, summary)

    yield _sse("research_complete", {
        "topic": filename,
        "session_id": session_id,
        "total_entries": len(state.all_entries),
        "total_ingested": state.total_ingested,
        "total_rejected": state.total_rejected,
        "new": state.total_new,
        "versioned": state.total_versioned,
        "rejected": state.total_rejected,
        "skipped_hash": state.total_skipped_hash,
        "iterations": 1,
        "total_urls_searched": 1,
        "total_queries": 1,
        "duration_ms": elapsed_ms,
        "summary": summary,
        "domain": state.domain,
        "depth": "direct_pdf",
        "page_count": page_count,
        "extractor_used": used,
    })


async def run_research_pdf(
    pdf_bytes: bytes,
    filename: str,
    extractor: str = "auto",
    domain: str | None = None,
    model_overrides: dict | None = None,
):
    """Entry point for PDF-mode research. Yields SSE events like run_research()."""
    t0 = time.monotonic()
    research_domain = domain or _detect_domain(filename)

    existing = await _guard_concurrent()
    if existing:
        yield _sse("error", {
            "message": f"Research already in progress: '{existing['topic']}'",
            "existing_session": str(existing["id"]),
            "http_status": 409,
        })
        return

    session_id = await _create_session(filename, "direct_pdf", research_domain)

    state = ResearchState(
        topic=filename,
        depth="direct_pdf",
        domain=research_domain,
    )

    extract_model = get_model("model_verifier", model_overrides)
    summary_model = get_model("model_verifier", model_overrides)

    yield _sse("research_started", {
        "topic": filename,
        "depth": "direct_pdf",
        "domain": state.domain,
        "max_iterations": 1,
        "session_id": session_id,
        "mode": "direct_pdf",
        "bytes": len(pdf_bytes),
    })

    try:
        async for evt in _run_research_pdf_mode(
            pdf_bytes=pdf_bytes,
            filename=filename,
            extractor=extractor,
            state=state,
            session_id=session_id,
            extract_model=extract_model,
            summary_model=summary_model,
            t0=t0,
        ):
            yield evt
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.error("pdf_research_failed: session=%s error=%s", session_id, exc, exc_info=True)
        await _finalize_session(session_id, "failed", elapsed_ms)
        yield _sse("error", {
            "message": f"PDF research failed: {exc}",
            "session_id": session_id,
            "topic": filename,
        })
