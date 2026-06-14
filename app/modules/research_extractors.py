"""Extraction primitives for research_agent — URL/GitHub/OpenAPI ref parsing,
bounded HTTP fetch, PDF extraction, chunking, SearXNG response caching,
source scoring."""

from __future__ import annotations

import asyncio
import hashlib
import io
import ipaddress
import json
import logging
import re
import socket
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import pdfplumber
import trafilatura
from pypdf import PdfReader

from app.config import settings
from app.modules.gt_extractor import TOPIC_KEYWORDS
from app.utils.http_clients import get_generic_http_client
from app.utils.topic_detection import detect_topic_id

logger = logging.getLogger("scaffold.research.extractors")


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

# §17.503 — engine lists refreshed to engines that actually respond on this
# SearXNG instance (measured 2026-06-13). The prior lists leaned on `google`
# (Suspended: access denied), `bing`, `stackoverflow`, `pypi`, `crossref`,
# `semantic_scholar`, `google news`, `bing news` — all returning 0 here — so
# `it`/`science`/`news` research got near-zero curated results and fell back to
# whatever the (now-removed) `categories` param dragged in (MDN). Each category
# now carries a reliable general-web backbone (duckduckgo + startpage) plus its
# specialist engines. Revisit if the SearXNG engine roster changes.
CATEGORY_ENGINES: dict[str, str] = {
    "it": "github,duckduckgo,startpage",
    "science": "arxiv,google scholar,duckduckgo",
    "news": "duckduckgo news,duckduckgo",
    "general": "duckduckgo,startpage,brave",
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
    """Map research topic to Milvus partition domain via keyword scoring.

    §17.501 — pass ``default=0`` (a topic_id NOT in ``topic_to_domain``,
    whose keys are 1-6) so a topic that matches NO keywords falls through
    to ``settings.default_domain`` ("eng") via the ``.get`` fallback below.
    Previously ``default=1`` routed every keyword-less topic to topic_id 1
    → the "llm" partition, contradicting the documented ``default_domain``
    and stranding e.g. homelab/infra research in "llm" where domain-pinned
    queries miss it. (Cross-domain ``domain=None`` retrieval still found it,
    which is why the mis-routing was silent.)
    """
    topic_id = detect_topic_id(topic, TOPIC_KEYWORDS, default=0)
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


_GITHUB_REF_RE = re.compile(r"^[A-Za-z0-9._\-/]{1,128}$")


def _is_github_ref(s: str) -> bool:
    if not s.startswith("github:"):
        return False
    rest = s[len("github:"):].strip()
    # Strip optional @<ref> suffix before validating owner/repo shape.
    repo_part = rest.split("@", 1)[0]
    parts = repo_part.split("/")
    return len(parts) == 2 and all(parts) and "." not in parts[0]


def _parse_github_ref(s: str) -> tuple[str, str, str | None]:
    """Parse ``github:owner/repo[@<tag|sha>]``.

    Returns ``(owner, repo, ref_hint)``. ``ref_hint`` is ``None`` when no
    ``@<ref>`` is given (caller resolves to latest release / default
    branch). Raises ``ValueError`` on malformed input.
    """
    if not _is_github_ref(s):
        raise ValueError(f"Malformed GitHub ref: {s!r} (expected 'github:owner/repo[@<ref>]')")
    rest = s[len("github:"):].strip()
    if "@" in rest:
        repo_part, ref_hint = rest.split("@", 1)
        ref_hint = ref_hint.strip() or None
        if ref_hint is not None and not _GITHUB_REF_RE.match(ref_hint):
            raise ValueError(
                f"Malformed GitHub ref: {s!r} (ref_hint {ref_hint!r} fails "
                f"{_GITHUB_REF_RE.pattern})"
            )
    else:
        repo_part = rest
        ref_hint = None
    owner, repo = repo_part.split("/", 1)
    owner, repo = owner.strip(), repo.strip()
    if not owner or not repo:
        raise ValueError(f"Malformed GitHub ref: {s!r} (empty owner or repo)")
    return owner, repo, ref_hint


_HF_KINDS: frozenset[str] = frozenset({"model", "dataset", "paper", "space", "doc"})
_HF_ID_RE = re.compile(r"^[A-Za-z0-9._\-/]{1,128}$")


def _is_hf_ref(s: str) -> bool:
    if not s.startswith("hf:"):
        return False
    rest = s[len("hf:"):].strip()
    if "/" not in rest:
        return False
    kind, _ = rest.split("/", 1)
    return kind in _HF_KINDS


def _parse_hf_ref(s: str) -> tuple[str, str]:
    """Parse ``hf:<kind>/<id>`` → ``(kind, id_)``.

    ``kind`` ∈ {model, dataset, paper, space}. ``id_`` may contain slashes
    (e.g., ``microsoft/phi-2``) but is constrained to alphanumerics, `.`,
    `_`, `-`, `/`, capped at 128 chars.

    Raises ``ValueError`` on malformed input.
    """
    if not _is_hf_ref(s):
        raise ValueError(
            f"Malformed HF ref: {s!r} (expected 'hf:<kind>/<id>' where kind ∈ "
            f"{sorted(_HF_KINDS)})"
        )
    rest = s[len("hf:"):].strip()
    kind, id_ = rest.split("/", 1)
    id_ = id_.strip()
    if not id_:
        raise ValueError(f"Malformed HF ref: {s!r} (empty id)")
    if not _HF_ID_RE.match(id_):
        raise ValueError(f"Malformed HF ref: {s!r} (id {id_!r} fails {_HF_ID_RE.pattern})")
    return kind, id_


# Forum prefixes — value after the prefix is a free-form query string.
# We allow spaces / mixed case so users can write `so:list comprehension python`.

_ARXIV_ID_RE = re.compile(
    r"^[0-9]{4}\.[0-9]{4,5}(v[0-9]+)?$|^[a-z\-]+(\.[A-Za-z\-]+)?/[0-9]{7}$"
)


def _is_so_ref(s: str) -> bool:
    return s.startswith("so:") and bool(s[len("so:"):].strip())


def _parse_so_ref(s: str) -> str:
    if not _is_so_ref(s):
        raise ValueError(f"Malformed SO ref: {s!r} (expected 'so:<query>')")
    return s[len("so:"):].strip()


def _is_hn_ref(s: str) -> bool:
    return s.startswith("hn:") and bool(s[len("hn:"):].strip())


def _parse_hn_ref(s: str) -> str:
    if not _is_hn_ref(s):
        raise ValueError(f"Malformed HN ref: {s!r} (expected 'hn:<query>')")
    return s[len("hn:"):].strip()


# Allowlist locked in code (not config) so widening trust requires a
# code change, not an env-var override. Lowercased for case-insensitive
# compare.
REDDIT_ALLOWLIST_LOWER: frozenset[str] = frozenset({
    "machinelearning",
    "localllama",
})


def _is_reddit_ref(s: str) -> bool:
    if not s.startswith("reddit:"):
        return False
    rest = s[len("reddit:"):].strip()
    return ":" in rest and all(part.strip() for part in rest.split(":", 1))


def _parse_reddit_ref(s: str) -> tuple[str, str]:
    """Parse ``reddit:<subreddit>:<query>`` → ``(subreddit, query)``.

    Subreddit is checked against ``REDDIT_ALLOWLIST_LOWER``; anything
    outside is rejected at parse time. Case is preserved in the returned
    tuple (Reddit URLs are case-sensitive for display) but matched
    case-insensitively against the allowlist.
    """
    if not _is_reddit_ref(s):
        raise ValueError(
            f"Malformed Reddit ref: {s!r} (expected 'reddit:<subreddit>:<query>')"
        )
    rest = s[len("reddit:"):].strip()
    sub, query = rest.split(":", 1)
    sub = sub.strip()
    query = query.strip()
    if not query:
        raise ValueError(f"Malformed Reddit ref: {s!r} (empty query)")
    if sub.lower() not in REDDIT_ALLOWLIST_LOWER:
        raise ValueError(
            f"Subreddit {sub!r} not in allowlist "
            f"({sorted(REDDIT_ALLOWLIST_LOWER)})"
        )
    return sub, query


def _is_wiki_ref(s: str) -> bool:
    return s.startswith("wiki:") and bool(s[len("wiki:"):].strip())


def _parse_wiki_ref(s: str) -> str:
    if not _is_wiki_ref(s):
        raise ValueError(f"Malformed Wiki ref: {s!r} (expected 'wiki:<topic>')")
    return s[len("wiki:"):].strip()


def _is_arxiv_ref(s: str) -> bool:
    return s.startswith("arxiv:") and bool(s[len("arxiv:"):].strip())


def _parse_arxiv_ref(s: str) -> tuple[str, str]:
    """Parse ``arxiv:<value>[:full]`` → ``(mode, value)``.

    ``mode`` ∈ ``{"id", "id_full", "query"}``:

    - ``id``       — value matches the arXiv ID format (``YYYY.NNNNN``
      with optional ``vN`` suffix, or legacy ``cat/0501001``). Abstract
      only; existing default behavior.
    - ``id_full``  — same as ``id`` but with a ``:full`` suffix.
      Triggers the full-PDF ingest path (§17.123): fetches
      ``arxiv.org/pdf/<id>.pdf``, extracts via pypdf, chunks, ingests.
    - ``query``    — free-text search.

    ``:full`` after a non-ID value is malformed.
    """
    if not _is_arxiv_ref(s):
        raise ValueError(f"Malformed arXiv ref: {s!r} (expected 'arxiv:<id|query>[:full]')")
    value = s[len("arxiv:"):].strip()

    if value.endswith(":full"):
        candidate = value[:-len(":full")].strip()
        if _ARXIV_ID_RE.match(candidate):
            return ("id_full", candidate)
        raise ValueError(
            f"Malformed arXiv ref: {s!r} "
            "(`:full` suffix requires a valid arXiv ID prefix)"
        )

    mode = "id" if _ARXIV_ID_RE.match(value) else "query"
    return mode, value


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


_PRIVATE_HOSTNAMES = frozenset({
    "localhost", "localhost.localdomain",
    "0.0.0.0", "::", "ip6-localhost", "ip6-loopback",
})


def _is_public_host(url: str) -> tuple[bool, str]:
    """§17.93 SSRF guard — return (ok, reason) for a target URL.

    Rejects:
      - non-http(s) schemes (file://, gopher://, etc.)
      - literal private hostnames (localhost, 0.0.0.0, ip6-loopback)
      - hostnames that resolve to any IPv4/IPv6 address in:
        loopback, link-local, private (RFC1918, ULA), unspecified,
        reserved, or multicast space.

    ``settings.research_allow_private_hosts`` (default False) opts
    out for local-development scenarios. The opt-out applies to the
    full resolution check, not the scheme check — non-HTTP schemes
    are always rejected.
    """
    try:
        p = urlparse(url.strip())
    except Exception as e:
        return False, f"url_parse_failed: {e}"
    if p.scheme not in ("http", "https"):
        return False, f"non_http_scheme: {p.scheme!r}"
    host = (p.hostname or "").lower().strip()
    if not host:
        return False, "empty_hostname"
    if settings.research_allow_private_hosts:
        return True, "private_hosts_allowed_by_setting"
    if host in _PRIVATE_HOSTNAMES:
        return False, f"literal_private_hostname: {host!r}"
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        return False, f"dns_resolve_failed: {e}"
    for fam, _stype, _proto, _canon, sockaddr in infos:
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False, f"unparseable_resolved_ip: {ip_str!r}"
        if (
            ip.is_private or ip.is_loopback or ip.is_link_local
            or ip.is_reserved or ip.is_multicast or ip.is_unspecified
        ):
            return False, (
                f"resolved_to_private_ip: host={host!r} ip={ip_str!r} "
                f"flags=private:{ip.is_private},loopback:{ip.is_loopback},"
                f"link_local:{ip.is_link_local}"
            )
    return True, "public_host"


async def _fetch_url_bounded(url: str, max_bytes: int | None = None) -> str | None:
    """Stream-fetch with hard byte cap. Returns text or None on failure/cap."""
    # §17.93 — SSRF guard. The fetch helper is the choke point for every
    # /research url:, /research openapi:, and pre-fetch path; rejecting
    # here covers all three without forcing per-caller validation.
    ok, reason = _is_public_host(url)
    if not ok:
        logger.warning("url_fetch_rejected_ssrf: url=%s reason=%s", url, reason)
        return None
    cap = max_bytes or settings.research_max_url_bytes
    try:
        # Item 12 — shared persistent client; per-call timeout override.
        client = _ra().get_generic_http_client()
        async with client.stream(
            "GET", url,
            headers={"User-Agent": "ScaffoldEngine/1.0"},
            timeout=settings.research_url_fetch_timeout,
        ) as resp:
            # §17.93 — re-validate the FINAL URL after any redirects.
            # The generic client has follow_redirects=True for normal API
            # fetches; without this re-check, an attacker could redirect
            # an initially-public URL to a private IP (3xx hop) and bypass
            # the pre-check. resp.url is the post-redirect-chain URL.
            final_url = str(resp.url)
            if final_url != url:
                ok2, reason2 = _is_public_host(final_url)
                if not ok2:
                    logger.warning(
                        "url_fetch_rejected_ssrf_after_redirect: "
                        "initial=%s final=%s reason=%s",
                        url, final_url, reason2,
                    )
                    return None
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
    # §17.503 — default to the reliable general-web backbone (google/bing are
    # dead on this instance) for any unmapped category.
    return CATEGORY_ENGINES.get(category, "duckduckgo,startpage")


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


async def _bounded_extract(fn, pdf_bytes: bytes) -> tuple[str, int]:
    """Run a sync extractor off-loop with a wall-clock bound (§17.406).

    ``wait_for`` cancels our await on timeout; the thread keeps running until
    the lib returns (Python can't cancel threads), but the research session
    fails cleanly instead of hanging on a corrupt/adversarial PDF.
    """
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(fn, pdf_bytes),
            timeout=settings.research_pdf_extract_timeout,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"PDF text extraction exceeded "
            f"{settings.research_pdf_extract_timeout}s "
            f"(corrupt or adversarially large PDF)"
        ) from exc


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
        text_out, pages = await _bounded_extract(_m._extract_pypdf, pdf_bytes)
        return (text_out, pages, "pypdf", False)

    if extractor == "plumber":
        text_out, pages = await _bounded_extract(_m._extract_pdfplumber, pdf_bytes)
        return (text_out, pages, "plumber", False)

    text_out, pages = await _bounded_extract(_m._extract_pypdf, pdf_bytes)
    if len(text_out) >= _extract_threshold(pages):
        return (text_out, pages, "pypdf", False)

    logger.info(
        "pdf_extract_fallback: pypdf_chars=%d pages=%d threshold=%d",
        len(text_out), pages, _extract_threshold(pages),
    )
    plumber_text, _ = await _bounded_extract(_m._extract_pdfplumber, pdf_bytes)
    if len(plumber_text) >= _extract_threshold(pages):
        return (plumber_text, pages, "plumber", True)

    raise RuntimeError(
        f"PDF appears to be scanned or unreadable: "
        f"pypdf={len(text_out)} chars, plumber={len(plumber_text)} chars, pages={pages}"
    )
