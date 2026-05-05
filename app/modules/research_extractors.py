"""Extraction primitives for research_agent — URL/GitHub/OpenAPI ref parsing,
bounded HTTP fetch, PDF extraction, chunking, SearXNG response caching,
source scoring."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import re
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import pdfplumber
import trafilatura
from pypdf import PdfReader

from app.config import settings
from app.modules.gt_extractor import TOPIC_KEYWORDS
from app.utils.http_clients import get_generic_http_client
from app.utils.topic_detection import detect_topic_id

logger = logging.getLogger("scaffold.research")


def _ra():
    """Lazy lookup of the research_agent module so tests that patch
    ``app.modules.research_agent.X`` (e.g. ``get_generic_http_client``,
    ``_extract_pypdf``) affect calls made from this module.

    The split on 2026-05-05 moved several helpers out of research_agent;
    tests still target the research_agent namespace, so we resolve the
    relevant dependencies through it at call-time.
    """
    import app.modules.research_agent as _m  # local to avoid import cycle at module load
    return _m


# =============================================================================
# Module constants
# =============================================================================

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
    """Reliability score (0.0–1.0) based on URL domain.

    Item 11 — Matches on exact host or registrable-suffix only. Substring
    matching (``if domain_key in hostname``) is vulnerable to lookalike
    hostnames such as ``fake-github.com.evil.tld`` scoring as ``github.com``.
    """
    try:
        hostname = urlparse(url).hostname or ""
    except Exception:
        return DEFAULT_SOURCE_SCORE
    host = hostname.lower().removeprefix("www.")
    if not host:
        return DEFAULT_SOURCE_SCORE
    for domain_key, score in DOMAIN_SCORES.items():
        if host == domain_key or host.endswith("." + domain_key):
            return score
    return DEFAULT_SOURCE_SCORE


def _detect_domain(topic: str) -> str:
    """Map research topic to Milvus partition domain via keyword scoring."""
    topic_id = detect_topic_id(topic, TOPIC_KEYWORDS, default=1)
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
    """Fail-open robots.txt check.

    Item 12 — Uses shared persistent client with per-call timeout override.
    """
    try:
        p = urlparse(url)
        robots_url = f"{p.scheme}://{p.netloc}/robots.txt"
        client = _ra().get_generic_http_client()
        r = await client.get(robots_url, timeout=settings.research_fetch_timeout)
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
        # Item 12 — shared persistent client; per-call timeout override.
        client = _ra().get_generic_http_client()
        async with client.stream(
            "GET", url,
            headers={"User-Agent": "ScaffoldEngine/1.0"},
            timeout=settings.research_url_fetch_timeout,
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

    # Item 9 — final post-overlap split pass.
    # Prepending tail-overlap can push a chunk past ``max_chars``. Enforce
    # the documented guarantee by hard-splitting any oversized chunk here.
    final: list[str] = []
    for c in chunks:
        if len(c) <= max_chars:
            final.append(c)
            continue
        for i in range(0, len(c), max_chars):
            final.append(c[i:i + max_chars])
    return final


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

    _m = _ra()
    if extractor == "pypdf":
        text_out, pages = await asyncio.to_thread(_m._extract_pypdf, pdf_bytes)
        return (text_out, pages, "pypdf", False)

    if extractor == "plumber":
        text_out, pages = await asyncio.to_thread(_m._extract_pdfplumber, pdf_bytes)
        return (text_out, pages, "plumber", False)

    text_out, pages = await asyncio.to_thread(_m._extract_pypdf, pdf_bytes)
    if len(text_out) >= _extract_threshold(pages):
        return (text_out, pages, "pypdf", False)

    logger.info(
        "pdf_extract_fallback: pypdf_chars=%d pages=%d threshold=%d",
        len(text_out), pages, _extract_threshold(pages),
    )
    plumber_text, _ = await asyncio.to_thread(_m._extract_pdfplumber, pdf_bytes)
    if len(plumber_text) >= _extract_threshold(pages):
        return (plumber_text, pages, "plumber", True)

    raise RuntimeError(
        f"PDF appears to be scanned or unreadable: "
        f"pypdf={len(text_out)} chars, plumber={len(plumber_text)} chars, pages={pages}"
    )
