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
import hashlib
import io
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import AsyncGenerator
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import httpx
import pdfplumber
import trafilatura
from pypdf import PdfReader
from sqlalchemy import text

from app import model_router
from app.config import settings, get_model
from app.database import async_session
from app.modules.rag_pipeline import ingest_entries
from app.utils.llm_parsing import parse_json_array, parse_json_object
from app.utils.topic_detection import detect_topic_id

logger = logging.getLogger("scaffold.research")


# =============================================================================
# Module constants
# =============================================================================

HEARTBEAT_INTERVAL_SECONDS = settings.research_heartbeat_interval
SEARXNG_CACHE_TTL_SECONDS = 3600

DOMAIN_SCORES: dict[str, float] = {
    "arxiv.org": 0.95, "ieee.org": 0.95, "acm.org": 0.95,
    "docs.python.org": 0.90, "docs.microsoft.com": 0.90,
    "learn.microsoft.com": 0.90, "developer.mozilla.org": 0.90,
    "kubernetes.io": 0.90, "docs.docker.com": 0.90,
    "pytorch.org": 0.90, "huggingface.co": 0.90,
    "github.com": 0.80, "stackoverflow.com": 0.80,
    "wiki.archlinux.org": 0.80,
    "medium.com": 0.60, "dev.to": 0.60, "towardsdatascience.com": 0.60,
    "reddit.com": 0.50,
}
DEFAULT_SOURCE_SCORE = 0.50

CATEGORY_ENGINES: dict[str, str] = {
    "it": "github,stackoverflow,pypi,google",
    "science": "arxiv,crossref,semantic_scholar,google",
    "news": "google news,bing news",
    "general": "google,bing,duckduckgo,brave",
}

_EXTRACT_BATCH_FULL_PAGE = 5
_EXTRACT_BATCH_SNIPPET = 10


# =============================================================================
# Helpers: source scoring, domain detection, confidence resolution
# =============================================================================

def _score_source(url: str) -> float:
    """Reliability score (0.0–1.0) based on URL domain."""
    try:
        hostname = urlparse(url).hostname or ""
    except Exception:
        return DEFAULT_SOURCE_SCORE
    hostname = hostname.lower().removeprefix("www.")
    for domain_key, score in DOMAIN_SCORES.items():
        if domain_key in hostname:
            return score
    return DEFAULT_SOURCE_SCORE


def _detect_domain(topic: str) -> str:
    """Map research topic to Milvus partition domain via keyword scoring."""
    topic_id = detect_topic_id(topic)
    return settings.topic_to_domain.get(topic_id, settings.default_domain)


def _resolve_confidence(entry_value, source_url: str) -> float:
    """Prefer LLM-provided confidence if valid [0.0, 1.0]; fall back to URL heuristic.

    Logs a warning when the LLM value is out of range.
    """
    if isinstance(entry_value, (int, float)):
        v = float(entry_value)
        if 0.0 <= v <= 1.0:
            return v
        logger.warning(
            "confidence_out_of_range: got=%s url=%s falling_back_to_url_score",
            entry_value, source_url,
        )
    return _score_source(source_url)


# =============================================================================
# ResearchState
# =============================================================================

@dataclass
class ResearchState:
    topic: str
    depth: str = "medium"
    domain: str = "eng"
    iteration: int = 0
    paused: bool = False
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


# =============================================================================
# SSE + heartbeat
# =============================================================================

def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"


async def _await_with_heartbeat(
    task: asyncio.Task,
    heartbeat_payload: dict,
    interval: int | None = None,
) -> AsyncGenerator[str, None]:
    """Yield heartbeat SSE while `task` is running. Caller reads task.result() after."""
    ivl = interval or HEARTBEAT_INTERVAL_SECONDS
    while not task.done():
        await asyncio.sleep(ivl)
        if not task.done():
            yield _sse("heartbeat", heartbeat_payload)


# =============================================================================
# Session tracking
# =============================================================================

async def _guard_concurrent() -> dict | None:
    """Return existing running-session dict or None."""
    async with async_session() as db:
        row = await db.execute(
            text("SELECT id, topic FROM research_sessions WHERE status = 'running' LIMIT 1")
        )
        existing = row.mappings().first()
        return dict(existing) if existing else None


async def _create_session(topic: str, depth: str, domain: str) -> str:
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


def _build_snapshot(state: ResearchState) -> dict:
    """JSON-safe snapshot of ResearchState for persistence."""
    return {
        "iteration": state.iteration,
        "search_history": sorted(state.search_history),
        "url_history": sorted(state.url_history),
        "entries_projection": [
            {
                "title": e.get("title", ""),
                "content_hash": e.get("content_hash")
                    or hashlib.sha256((e.get("content") or "").encode("utf-8")).hexdigest()[:16],
            }
            for e in state.all_entries
        ],
        "outline_facets": state.outline_facets,
        "covered_facets": sorted(state.covered_facets),
        "gap_queries": state.gap_queries,
        "totals": {
            "ingested": state.total_ingested,
            "rejected": state.total_rejected,
            "new": state.total_new,
            "versioned": state.total_versioned,
            "skipped_hash": state.total_skipped_hash,
        },
    }


async def _update_session_iteration(
    session_id: str,
    state: ResearchState,
    coverage: float | None = None,
) -> None:
    snapshot = _build_snapshot(state)
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
                    coverage_pct = COALESCE(:coverage, coverage_pct),
                    state_snapshot = CAST(:snapshot AS JSONB),
                    updated_at = NOW()
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
                "snapshot": json.dumps(snapshot),
            },
        )
        await db.commit()


async def _pause_session(
    session_id: str,
    state: ResearchState,
    question: str,
    ttl_seconds: int = 3600,
) -> None:
    snapshot = _build_snapshot(state)
    async with async_session() as db:
        await db.execute(
            text("""
                UPDATE research_sessions
                SET status = 'paused_awaiting_reply',
                    pause_question = :question,
                    pause_expires_at = NOW() + make_interval(secs => :ttl),
                    state_snapshot = CAST(:snapshot AS JSONB),
                    updated_at = NOW()
                WHERE id = :sid
            """),
            {
                "sid": session_id,
                "question": question,
                "ttl": ttl_seconds,
                "snapshot": json.dumps(snapshot),
            },
        )
        await db.commit()


async def _load_session_for_resume(session_id: str) -> dict | None:
    async with async_session() as db:
        row = await db.execute(
            text("""
                SELECT id, topic, depth, domain, status, state_snapshot,
                       pause_question, pause_expires_at, pause_reply
                FROM research_sessions
                WHERE id = :sid
            """),
            {"sid": session_id},
        )
        r = row.mappings().first()
        return dict(r) if r else None


async def _atomic_claim_for_resume(session_id: str, reply: str) -> bool:
    """Atomic paused_awaiting_reply → running. Returns True if this caller won the race."""
    async with async_session() as db:
        result = await db.execute(
            text("""
                UPDATE research_sessions
                SET status = 'running',
                    pause_reply = :reply,
                    updated_at = NOW()
                WHERE id = :sid
                  AND status = 'paused_awaiting_reply'
            """),
            {"sid": session_id, "reply": reply},
        )
        await db.commit()
        return result.rowcount == 1


def _rehydrate_state(row: dict) -> ResearchState:
    snap = row.get("state_snapshot") or {}
    if isinstance(snap, str):
        snap = json.loads(snap) if snap else {}

    state = ResearchState(
        topic=row["topic"],
        depth=row["depth"],
        domain=row["domain"],
    )
    state.iteration = int(snap.get("iteration", 0))
    state.search_history = set(snap.get("search_history", []))
    state.url_history = set(snap.get("url_history", []))
    state.outline_facets = list(snap.get("outline_facets", []))
    state.covered_facets = set(snap.get("covered_facets", []))
    state.gap_queries = list(snap.get("gap_queries", []))
    state.all_entries = list(snap.get("entries_projection", []))
    totals = snap.get("totals", {})
    state.total_ingested = int(totals.get("ingested", 0))
    state.total_rejected = int(totals.get("rejected", 0))
    state.total_new = int(totals.get("new", 0))
    state.total_versioned = int(totals.get("versioned", 0))
    state.total_skipped_hash = int(totals.get("skipped_hash", 0))
    return state


async def _finalize_session(
    session_id: str,
    status: str,
    duration_ms: int,
    summary: str | None = None,
    error_message: str | None = None,
) -> None:
    async with async_session() as db:
        await db.execute(
            text("""
                UPDATE research_sessions
                SET status = :status,
                    completed_at = NOW(),
                    duration_ms = :dur,
                    summary = :summary,
                    error_message = COALESCE(:error_message, error_message),
                    updated_at = NOW()
                WHERE id = :sid
            """),
            {
                "sid": session_id,
                "status": status,
                "dur": duration_ms,
                "summary": summary,
                "error_message": error_message,
            },
        )
        await db.commit()


# =============================================================================
# URL / GitHub / OpenAPI parsing + fetching
# =============================================================================

def _is_url(s: str) -> bool:
    try:
        p = urlparse(s.strip())
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False


def _is_github_ref(s: str) -> bool:
    if not s.startswith("github:"):
        return False
    rest = s[len("github:"):].strip()
    parts = rest.split("/")
    return len(parts) == 2 and all(parts) and "." not in parts[0]


def _parse_github_ref(s: str) -> tuple[str, str]:
    """Parse `github:owner/repo`. Raises ValueError on malformed input."""
    if not _is_github_ref(s):
        raise ValueError(f"Malformed GitHub ref: {s!r} (expected 'github:owner/repo')")
    owner, repo = s[len("github:"):].strip().split("/", 1)
    owner, repo = owner.strip(), repo.strip()
    if not owner or not repo:
        raise ValueError(f"Malformed GitHub ref: {s!r} (empty owner or repo)")
    return owner, repo


def _is_openapi_ref(s: str) -> bool:
    if not s.startswith("openapi:"):
        return False
    rest = s[len("openapi:"):].strip()
    return rest.startswith("http://") or rest.startswith("https://")


def _parse_openapi_ref(s: str) -> str:
    if not _is_openapi_ref(s):
        raise ValueError(f"Malformed OpenAPI ref: {s!r}")
    return s[len("openapi:"):].strip()


async def _robots_allowed(url: str, user_agent: str = "ScaffoldEngine/1.0") -> bool:
    """Fail-open robots.txt check."""
    try:
        p = urlparse(url)
        robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
        async with httpx.AsyncClient(
            timeout=settings.research_fetch_timeout, follow_redirects=True,
        ) as c:
            r = await c.get(robots_url)
            if r.status_code >= 400:
                return True
            rp = RobotFileParser()
            rp.parse(r.text.splitlines())
            return rp.can_fetch(user_agent, url)
    except Exception as e:
        logger.debug("robots_check_failed: url=%s error=%s", url, e)
        return True


async def _fetch_url_bounded(url: str, max_bytes: int | None = None) -> str | None:
    """Stream-fetch with hard byte cap. Returns text or None on failure/cap."""
    cap = max_bytes or settings.research_max_url_bytes
    try:
        async with httpx.AsyncClient(
            timeout=settings.research_url_fetch_timeout, follow_redirects=True,
        ) as c:
            async with c.stream(
                "GET", url, headers={"User-Agent": "ScaffoldEngine/1.0"},
            ) as resp:
                if resp.status_code != 200:
                    logger.warning("url_fetch_status: url=%s status=%d", url, resp.status_code)
                    return None
                cl = resp.headers.get("content-length")
                if cl and cl.isdigit() and int(cl) > cap:
                    logger.warning("url_fetch_content_length_exceeded: url=%s bytes=%s", url, cl)
                    return None
                buf = bytearray()
                async for chunk in resp.aiter_bytes():
                    buf.extend(chunk)
                    if len(buf) > cap:
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


async def _extract_page_title(html: str, url: str) -> str:
    """trafilatura metadata → <title> regex → URL. Always returns a string."""
    try:
        meta = await asyncio.to_thread(trafilatura.extract_metadata, html)
        if meta:
            title = getattr(meta, "title", None)
            if title:
                return str(title).strip()[:200]
    except Exception as e:
        logger.debug("trafilatura_metadata_failed: url=%s error=%s", url, e)

    match = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    if match:
        title = re.sub(r"\s+", " ", match.group(1)).strip()
        if title:
            return title[:200]

    return url[:200]


# =============================================================================
# Chunking
# =============================================================================

def _chunk_text(text_in: str, max_tokens: int = 1500, overlap_tokens: int = 200) -> list[str]:
    """Paragraph-aware chunking (~4 chars/token). Oversized paragraphs hard-split.

    Guarantees no chunk exceeds max_chars.
    """
    max_chars = max_tokens * 4
    overlap_chars = overlap_tokens * 4

    if len(text_in) <= max_chars:
        return [text_in]

    # Pre-split oversized paragraphs
    paragraphs: list[str] = []
    for p in text_in.split("\n\n"):
        if len(p) <= max_chars:
            paragraphs.append(p)
            continue
        pieces = re.split(r"(?<=[.!?])\s+", p)
        buf = ""
        for piece in pieces:
            if len(piece) > max_chars:
                for i in range(0, len(piece), max_chars):
                    paragraphs.append(piece[i:i + max_chars])
                continue
            if len(buf) + len(piece) + 1 <= max_chars:
                buf = f"{buf} {piece}" if buf else piece
            else:
                if buf:
                    paragraphs.append(buf)
                buf = piece
        if buf:
            paragraphs.append(buf)

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


# =============================================================================
# SearXNG cache + engine routing
# =============================================================================

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
        await r.setex(_searxng_cache_key(query), SEARXNG_CACHE_TTL_SECONDS, json.dumps(results))
    except Exception as e:
        logger.debug("searxng_cache_set_failed: query=%s error=%s", query, e)


def _engines_for_category(category: str) -> str:
    return CATEGORY_ENGINES.get(category, "google,bing,duckduckgo")


# =============================================================================
# PDF extraction
# =============================================================================

def _extract_pypdf(pdf_bytes: bytes) -> tuple[str, int]:
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
    return max(200, page_count * 50)


async def _extract_pdf_text(
    pdf_bytes: bytes,
    extractor: str = "auto",
) -> tuple[str, int, str, bool]:
    """Extract text. Returns (text, page_count, extractor_used, fell_back)."""
    extractor = (extractor or "auto").lower()
    if extractor not in ("auto", "pypdf", "plumber"):
        extractor = "auto"

    if extractor == "pypdf":
        text_out, pages = await asyncio.to_thread(_extract_pypdf, pdf_bytes)
        return (text_out, pages, "pypdf", False)

    if extractor == "plumber":
        text_out, pages = await asyncio.to_thread(_extract_pdfplumber, pdf_bytes)
        return (text_out, pages, "plumber", False)

    text_out, pages = await asyncio.to_thread(_extract_pypdf, pdf_bytes)
    if len(text_out) >= _extract_threshold(pages):
        return (text_out, pages, "pypdf", False)

    logger.info(
        "pdf_extract_fallback: pypdf_chars=%d pages=%d threshold=%d",
        len(text_out), pages, _extract_threshold(pages),
    )
    plumber_text, _ = await asyncio.to_thread(_extract_pdfplumber, pdf_bytes)
    if len(plumber_text) >= _extract_threshold(pages):
        return (plumber_text, pages, "plumber", True)

    raise RuntimeError(
        f"PDF appears to be scanned or unreadable: "
        f"pypdf={len(text_out)} chars, plumber={len(plumber_text)} chars, pages={pages}"
    )


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

    async with httpx.AsyncClient(
        timeout=settings.research_fetch_timeout, follow_redirects=True,
    ) as client:
        async def _fetch_one(url: str) -> dict | None:
            async with sem:
                try:
                    resp = await client.get(url)
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


# =============================================================================
# Decomposition, search, extraction, gap analysis, summary
# =============================================================================

async def _decompose_topic(
    topic: str,
    model: str,
    existing_facets: list | None = None,
    gap_focus: str | None = None,
) -> dict:
    """Decompose topic into queries. Retries once on <2 facets; falls back."""
    prompt = f"Decompose this research topic into search queries:\n\nTOPIC: {topic}"
    if existing_facets:
        prompt += f"\n\nAlready covered facets (do NOT repeat): {', '.join(existing_facets)}"
    if gap_focus:
        prompt += f"\n\nFocus specifically on these gaps: {gap_focus}"

    resp = await model_router.generate(
        prompt, model=model, system=DECOMPOSE_SYSTEM_V1,
        temperature=0.4, max_tokens=2048,
    )

    if resp.success:
        parsed = parse_json_object(resp.text)
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
            retry_resp = await model_router.generate(
                retry_prompt, model=model, system=DECOMPOSE_SYSTEM_V1,
                temperature=0.5, max_tokens=2048,
            )
            if retry_resp.success:
                retry_parsed = parse_json_object(retry_resp.text)
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
    model: str,
) -> list[dict]:
    """Distill search results into knowledge entries.

    Fetches full pages via trafilatura; chunks long pages; snippet fallback.
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

    expanded_results: list[dict] = []
    for r in results:
        url = r.get("url", "")
        full = url_to_text.get(url)
        if full:
            for chunk in _chunk_text(full):
                expanded_results.append({
                    "title": r.get("title", ""),
                    "url": url,
                    "content": chunk,
                    "facet": r.get("facet", ""),
                })
        else:
            expanded_results.append(r)

    batch_size = _EXTRACT_BATCH_FULL_PAGE if fetched else _EXTRACT_BATCH_SNIPPET
    all_entries: list[dict] = []

    for i in range(0, len(expanded_results), batch_size):
        batch = expanded_results[i:i + batch_size]
        results_text = "\n\n".join(
            f"Title: {r['title']}\nURL: {r['url']}\nSnippet: {r['content'][:600]}"
            for r in batch
        )
        resp = await model_router.generate(
            EXTRACT_PROMPT_V1.format(topic=topic, results=results_text),
            model=model, system=EXTRACT_SYSTEM_V1,
            temperature=0.1, max_tokens=1024,
        )

        entries: list[dict] = []
        if resp.success and resp.text and len(resp.text.strip()) > 5:
            parsed = parse_json_array(resp.text) or []
            entries = [e for e in parsed if isinstance(e, dict)]
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
    model: str,
) -> dict:
    """Analyze coverage gaps. Retries once on parse failure."""
    entry_summaries = [
        f"[{e.get('facet', '?')}] {e.get('title', '')}: {e.get('content', '')[:100]}"
        for e in state.all_entries[-50:]
    ]

    prompt = (
        f"Topic: {state.topic}\n"
        f"Expected facets: {', '.join(state.outline_facets)}\n"
        f"Entries collected: {len(state.all_entries)}\n"
        f"Iterations completed: {state.iteration}\n\n"
        f"Sample entries:\n" + "\n".join(entry_summaries[:30])
    )

    for attempt in range(2):
        resp = await model_router.generate(
            prompt, model=model, system=GAP_SYSTEM_V1,
            temperature=0.3, max_tokens=2048,
        )
        if resp.success:
            parsed = parse_json_object(resp.text)
            if parsed:
                return parsed
        if attempt == 0:
            logger.info("gap_analysis_retry: attempt 1 failed, retrying")

    return {
        "coverage_pct": 100,
        "covered_facets": state.outline_facets,
        "gap_facets": [],
        "gap_queries": [],
        "assessment": "Gap analysis failed — treating as complete.",
    }


async def _generate_summary(
    state: ResearchState,
    model: str,
) -> str:
    """Generate human-readable summary of all collected research."""
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
        prompt, model=model, system=SUMMARY_SYSTEM_V1,
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
    decompose_model: str,
    extract_model: str,
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
            _extract_entries(results, topic, model=extract_model)
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

        await _update_session_iteration(session_id, state, coverage)

        if state.iteration >= state.max_iterations:
            break

        if ingested == 0 and len(entries) > 0:
            yield _sse("convergence", {
                "reason": "all_duplicates",
                "message": "All extracted entries were duplicates.",
            })
            break

        gap_task = asyncio.create_task(_analyze_gaps(state, model=decompose_model))
        async for hb in _await_with_heartbeat(
            gap_task, {"status": "analyzing_gaps"}
        ):
            yield hb
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
    summary_model: str | None = None,
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

    ingested = 0
    if entries:
        stats = await ingest_entries(entries, domain=state.domain)
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
    if summary_model is not None and state.all_entries:
        summary_task = asyncio.create_task(
            _generate_summary(state, model=summary_model)
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
) -> AsyncGenerator[str, None]:
    """GitHub-mode: fetch README + docs + docstrings, ingest as tech_docs."""
    from app.utils.github_ingest import fetch_repo_content

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
    })

    task = asyncio.create_task(fetch_repo_content(owner, repo))
    async for hb in _await_with_heartbeat(
        task, {"status": "fetching_github", "iteration": 1}
    ):
        yield hb
    files = task.result()

    if not files:
        raise RuntimeError(f"No ingestible content found in {owner}/{repo}")

    yield _sse("search_complete", {
        "iteration": 1,
        "results_found": len(files),
        "total_urls": len(files),
        "mode": "github",
    })

    entries: list[dict] = []
    for f in files:
        source_url = f"https://github.com/{owner}/{repo}/blob/HEAD/{f['path']}"
        state.url_history.add(source_url)
        entries.append({
            "title": f"{owner}/{repo}: {f['path']}",
            "content": f["content"],
            "source": source_url,
            "source_type": "tech_docs",
            "confidence_score": 0.9,
            "facet": "github_repo",
        })

    yield _sse("extraction_complete", {
        "iteration": 1,
        "entries_extracted": len(entries),
        "mode": "github",
    })

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


async def _run_research_url_mode(
    url: str,
    state: ResearchState,
    session_id: str,
    extract_model: str,
    summary_model: str,
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

    for i in range(0, len(chunks), batch_size):
        batch_chunks = chunks[i:i + batch_size]
        results_text = "\n\n".join(
            f"Title: {page_title}\nURL: {url}\nSnippet: {c[:600]}"
            for c in batch_chunks
        )
        task = asyncio.create_task(model_router.generate(
            EXTRACT_PROMPT_V1.format(topic=prompt_topic, results=results_text),
            model=extract_model,
            system=EXTRACT_SYSTEM_V1,
            temperature=0.1,
            max_tokens=1024,
        ))
        async for hb in _await_with_heartbeat(
            task, {"status": "extracting", "iteration": 1}
        ):
            yield hb
        resp = task.result()

        batch_entries: list[dict] = []
        if resp.success and resp.text and len(resp.text.strip()) > 5:
            parsed = parse_json_array(resp.text) or []
            batch_entries = [e for e in parsed if isinstance(e, dict)]
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
            logger.warning(
                "url_mode_extract_failed: batch=%d success=%s error=%s",
                i // batch_size,
                resp.success if resp else None,
                resp.error if resp else "no-resp",
            )

        if not batch_entries:
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

    yield _sse("extraction_complete", {
        "iteration": 1,
        "entries_extracted": len(entries),
    })

    async for evt in _ingest_and_finalize_direct(
        state=state,
        session_id=session_id,
        entries=entries,
        mode="direct_url",
        topic=url,
        t0=t0,
        summary_model=summary_model,
    ):
        yield evt


async def _run_research_pdf_mode(
    pdf_bytes: bytes,
    filename: str,
    extractor: str,
    state: ResearchState,
    session_id: str,
    extract_model: str,
    summary_model: str,
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
        task = asyncio.create_task(model_router.generate(
            EXTRACT_PROMPT_V1.format(topic=filename, results=results_text),
            model=extract_model,
            system=EXTRACT_SYSTEM_V1,
            temperature=0.1,
            max_tokens=1024,
        ))
        async for hb in _await_with_heartbeat(
            task, {"status": "extracting", "iteration": 1}
        ):
            yield hb
        resp = task.result()

        batch_entries: list[dict] = []
        if resp.success and resp.text and len(resp.text.strip()) > 5:
            parsed = parse_json_array(resp.text) or []
            batch_entries = [e for e in parsed if isinstance(e, dict)]
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
            logger.warning(
                "pdf_mode_extract_failed: batch=%d success=%s error=%s",
                i // batch_size,
                resp.success if resp else None,
                resp.error if resp else "no-resp",
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

    async for evt in _ingest_and_finalize_direct(
        state=state,
        session_id=session_id,
        entries=entries,
        mode="direct_pdf",
        topic=filename,
        t0=t0,
        summary_model=summary_model,
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
    """Execute research, yielding SSE events. Dispatches to direct modes by prefix."""
    t0 = time.monotonic()
    research_domain = domain or _detect_domain(topic)

    existing = await _guard_concurrent()
    if existing:
        yield _sse("error", {
            "message": f"Research already in progress: '{existing['topic']}'",
            "existing_session": str(existing["id"]),
            "http_status": 409,
        })
        return

    # Determine mode + persisted depth label
    if _is_openapi_ref(topic):
        mode, state_depth = "openapi", "direct_openapi"
    elif _is_github_ref(topic):
        mode, state_depth = "github", "direct_github"
    elif _is_url(topic):
        mode, state_depth = "direct_url", "direct_url"
    else:
        mode, state_depth = "topic", depth

    session_id = await _create_session(topic, state_depth, research_domain)
    state = ResearchState(topic=topic, depth=state_depth, domain=research_domain)

    # --- Direct modes: single-iteration, protected by try/except for #4/#5 ---
    if mode != "topic":
        yield _sse("research_started", {
            "topic": topic,
            "depth": state_depth,
            "domain": state.domain,
            "max_iterations": 1,
            "session_id": session_id,
            "mode": mode,
        })
        try:
            if mode == "openapi":
                async for evt in _run_research_openapi_mode(
                    _parse_openapi_ref(topic), state, session_id, t0,
                ):
                    yield evt
            elif mode == "github":
                owner, repo = _parse_github_ref(topic)
                async for evt in _run_research_github_mode(
                    owner, repo, state, session_id, t0,
                ):
                    yield evt
            elif mode == "direct_url":
                extract_m = get_model("model_verifier", model_overrides)
                summary_m = get_model("model_verifier", model_overrides)
                async for evt in _run_research_url_mode(
                    topic, state, session_id, extract_m, summary_m, t0,
                ):
                    yield evt
        except Exception as exc:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            logger.error(
                "direct_mode_failed: mode=%s session=%s error=%s",
                mode, session_id, exc, exc_info=True,
            )
            await _finalize_session(
                session_id, "failed", elapsed_ms,
                error_message=f"{type(exc).__name__}: {exc}",
            )
            yield _sse("error", {
                "message": f"Research failed: {exc}",
                "session_id": session_id,
                "topic": topic,
            })
        return

    # --- Topic mode ---
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
        decomposition = await _decompose_topic(topic, model=decompose_model)
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
            decompose_model=decompose_model,
            extract_model=extract_model,
            topic=topic,
            allow_pause=True,
        ):
            yield evt

        if state.paused:
            return

        summary_task = asyncio.create_task(
            _generate_summary(state, model=summary_model)
        )
        async for hb in _await_with_heartbeat(
            summary_task, {"status": "summarizing"}
        ):
            yield hb
        summary = summary_task.result()
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
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.error(
            "research_failed: session=%s error=%s",
            session_id, exc, exc_info=True,
        )
        await _finalize_session(
            session_id, "failed", elapsed_ms,
            error_message=f"{type(exc).__name__}: {exc}",
        )
        yield _sse("error", {
            "message": f"Research failed: {exc}",
            "session_id": session_id,
            "topic": topic,
        })


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
    decompose_model = get_model("model_verifier", model_overrides)
    extract_model = get_model("model_verifier", model_overrides)
    summary_model = get_model("model_verifier", model_overrides)

    yield _sse("research_resumed", {
        "session_id": session_id,
        "topic": topic,
        "iteration": state.iteration,
        "reply": reply,
    })

    try:
        # #142: targeted decompose with reply as gap_focus (replaces 2-query seed)
        decomposition = await _decompose_topic(
            topic,
            model=decompose_model,
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
            decompose_model=decompose_model,
            extract_model=extract_model,
            topic=topic,
            allow_pause=False,
        ):
            yield evt

        summary_task = asyncio.create_task(
            _generate_summary(state, model=summary_model)
        )
        async for hb in _await_with_heartbeat(
            summary_task, {"status": "summarizing"}
        ):
            yield hb
        summary = summary_task.result()
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
    except Exception as exc:
        elapsed_ms = int((time.monotonic() - t0) * 1000)
        logger.error(
            "resume_research_failed: session=%s error=%s",
            session_id, exc, exc_info=True,
        )
        await _finalize_session(
            session_id, "failed", elapsed_ms,
            error_message=f"Resume failed: {type(exc).__name__}: {exc}",
        )
        yield _sse("error", {
            "message": f"Resume failed: {exc}",
            "session_id": session_id,
            "topic": topic,
        })


async def run_research_pdf(
    pdf_bytes: bytes,
    filename: str,
    extractor: str = "auto",
    domain: str | None = None,
    model_overrides: dict | None = None,
) -> AsyncGenerator[str, None]:
    """Entry point for PDF research (called from /research/pdf endpoint)."""
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
        topic=filename, depth="direct_pdf", domain=research_domain,
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
        logger.error(
            "pdf_research_failed: session=%s error=%s",
            session_id, exc, exc_info=True,
        )
        await _finalize_session(
            session_id, "failed", elapsed_ms,
            error_message=f"{type(exc).__name__}: {exc}",
        )
        yield _sse("error", {
            "message": f"PDF research failed: {exc}",
            "session_id": session_id,
            "topic": filename,
        })
