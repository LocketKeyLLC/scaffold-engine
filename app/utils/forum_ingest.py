"""Forum-mode ingestion: Stack Overflow, Hacker News, arXiv.

Each prefix maps to a public REST endpoint, each fetcher returns the
same entry-dict shape used by github_ingest / hf_ingest so the
research-agent loop can compose them uniformly.

| Prefix     | API                                      | source_type      |
|------------|------------------------------------------|------------------|
| ``so:``    | api.stackexchange.com (StackOverflow)    | ``so_answer``    |
| ``hn:``    | hn.algolia.com (HN Algolia search)       | ``hn_comment``   |
| ``arxiv:`` | export.arxiv.org/api/query (Atom XML)    | ``paper_abstract`` |

All three sources are public / unauthenticated. The Stack Exchange free
quota is 300 req/day anonymous (10k/day with an app key); HN Algolia
and arXiv are unmetered. Quality gates filter at fetch time:

- SO: ``is_accepted=True`` OR ``score >= so_min_score`` (default 10).
- HN: ``points >= hn_min_points`` (default 100).
- arXiv: abstract only by default — full PDFs run through the existing
  ``/research/pdf`` pipeline if the operator wants them.

PII strip pass (``_strip_pii``) collapses ``@username`` mentions and
emails before ingest. The body still records the source URL in
provenance, so attribution is preserved without indexing personal handles.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import xml.etree.ElementTree as ET
from typing import Any

import httpx

from app.config import settings
from app.utils.fetch_cache import get_fetch_cache
from app.utils.http_clients import get_generic_http_client

logger = logging.getLogger(__name__)


# Stack Exchange / HN / arXiv base URLs. Held here (not in config) because
# they're functional constants — operators don't override these like they
# might override an Ollama base URL.
_SE_BASE = "https://api.stackexchange.com/2.3"
_HN_BASE = "https://hn.algolia.com/api/v1"
_ARXIV_BASE = "http://export.arxiv.org/api/query"
_REDDIT_BASE = "https://www.reddit.com"
_WIKI_BASE = "https://en.wikipedia.org/w/api.php"

# `@` not preceded by a word char — keeps the email-replacement placeholder
# from being matched again by the username pass.
_AT_USER_RE = re.compile(r"(?<!\w)@[A-Za-z0-9_\-]+")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _strip_pii(text: str) -> str:
    """Replace @username mentions and emails with redacted placeholders."""
    text = _EMAIL_RE.sub("email@redacted", text)
    text = _AT_USER_RE.sub("@user", text)
    return text


def _strip_html(html: str) -> str:
    """Crude HTML→text conversion. SE bodies arrive as HTML; we drop tags
    and unescape a few common entities without pulling in a full parser.
    """
    text = _HTML_TAG_RE.sub("", html)
    text = (text.replace("&amp;", "&")
                .replace("&lt;", "<")
                .replace("&gt;", ">")
                .replace("&quot;", '"')
                .replace("&#39;", "'")
                .replace("&nbsp;", " "))
    return text


def _query_hash(query: str) -> str:
    return hashlib.sha256(query.lower().encode("utf-8")).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Stack Overflow
# ---------------------------------------------------------------------------

async def fetch_so_answers(query: str, limit: int, min_score: int) -> list[dict[str, Any]]:
    """Search SO for questions matching ``query``, return the
    accepted-or-high-score answers as entries.

    Quality gate: each kept answer is ``is_accepted=True`` OR
    ``score >= min_score``. Bodies HTML-stripped + PII-redacted.
    Individual answer bodies cached by ``answer_id`` (immutable TTL) so
    repeat queries that overlap on top answers skip the network.
    """
    if limit <= 0:
        return []
    client = get_generic_http_client()

    # 1. Search questions. `filter=withbody` returns question text but not
    #    answer bodies — we still need an answer-batch call below.
    search_url = (
        f"{_SE_BASE}/search/advanced"
        f"?order=desc&sort=votes&q={httpx.QueryParams({'q': query})['q']}"
        f"&site=stackoverflow&accepted=True&pagesize={min(limit * 2, 50)}"
        f"&filter=withbody"
    )
    r = await client.get(search_url)
    if r.status_code == 429:
        logger.warning("so_rate_limited: %s", r.headers.get("Retry-After"))
        return []
    r.raise_for_status()
    questions = r.json().get("items", []) or []
    if not questions:
        return []

    # 2. Batch-fetch accepted answers. Filter for body + score.
    accepted_ids = [q["accepted_answer_id"] for q in questions if q.get("accepted_answer_id")]
    if not accepted_ids:
        return []

    cache = get_fetch_cache()
    answer_bodies: dict[int, dict] = {}
    missing: list[int] = []
    for aid in accepted_ids:
        cached = await cache.get("so", f"answer-{aid}", "body")
        if cached:
            try:
                answer_bodies[aid] = json.loads(cached)
                continue
            except Exception:
                pass
        missing.append(aid)

    if missing:
        ids_str = ";".join(str(i) for i in missing[:100])
        ans_url = (
            f"{_SE_BASE}/answers/{ids_str}"
            f"?site=stackoverflow&filter=withbody"
        )
        r = await client.get(ans_url)
        if r.status_code == 429:
            logger.warning("so_answers_rate_limited")
        else:
            r.raise_for_status()
            for a in r.json().get("items", []) or []:
                aid = a.get("answer_id")
                if not aid:
                    continue
                payload = {
                    "score": a.get("score", 0),
                    "is_accepted": bool(a.get("is_accepted")),
                    "body": a.get("body", ""),
                    "link": a.get("link", ""),
                    "creation_date": a.get("creation_date", 0),
                }
                answer_bodies[aid] = payload
                try:
                    await cache.put(
                        "so", f"answer-{aid}", "body",
                        json.dumps(payload).encode("utf-8"),
                        ttl_seconds=settings.fetch_cache_ttl_immutable_seconds,
                    )
                except Exception as exc:
                    logger.debug("so_cache_put_failed: %s", exc)

    # 3. Compose entries (question title + answer body), gated.
    out: list[dict[str, Any]] = []
    for q in questions:
        if len(out) >= limit:
            break
        aid = q.get("accepted_answer_id")
        if not aid:
            continue
        ans = answer_bodies.get(aid)
        if not ans:
            continue
        score = int(ans.get("score", 0))
        is_accepted = bool(ans.get("is_accepted"))
        if not (is_accepted or score >= min_score):
            continue
        body_text = _strip_pii(_strip_html(ans.get("body", ""))).strip()
        if not body_text:
            continue
        q_title = q.get("title", "")
        link = ans.get("link", f"https://stackoverflow.com/a/{aid}")
        out.append({
            "path": f"so/answer-{aid}",
            "content": f"# Q: {q_title}\n\n{body_text}",
            "source_type": "so_answer",
            "source_url": link,
            "source_ref": f"answer-{aid}",
            "quality_signal": {
                "score": score,
                "is_accepted": is_accepted,
                "question_score": int(q.get("score", 0)),
                "question_id": q.get("question_id", 0),
                "tags": q.get("tags") or [],
            },
        })
    return out


# ---------------------------------------------------------------------------
# Hacker News (Algolia)
# ---------------------------------------------------------------------------

async def fetch_hn_items(query: str, limit: int, min_points: int) -> list[dict[str, Any]]:
    """Search HN via Algolia for stories + comments matching ``query``.

    Quality gate: ``points >= min_points``. PII strip applied. Search
    response cached by query hash with short TTL (Algolia rankings drift).
    """
    if limit <= 0:
        return []
    client = get_generic_http_client()

    qhash = _query_hash(query)
    cache = get_fetch_cache()
    cached = await cache.get("hn", f"search-{qhash}", f"min{min_points}")
    if cached:
        try:
            data = json.loads(cached)
        except Exception:
            data = None
    else:
        data = None

    if data is None:
        params = {
            "query": query,
            "numericFilters": f"points>={min_points}",
            "hitsPerPage": str(min(limit * 2, 50)),
        }
        url = f"{_HN_BASE}/search?" + "&".join(
            f"{k}={httpx.QueryParams({k: v})[k]}" for k, v in params.items()
        )
        r = await client.get(url)
        r.raise_for_status()
        data = r.json()
        try:
            await cache.put(
                "hn", f"search-{qhash}", f"min{min_points}",
                json.dumps(data).encode("utf-8"),
                ttl_seconds=settings.fetch_cache_ttl_default_seconds,
            )
        except Exception as exc:
            logger.debug("hn_cache_put_failed: %s", exc)

    hits = (data or {}).get("hits", []) or []
    out: list[dict[str, Any]] = []
    for hit in hits:
        if len(out) >= limit:
            break
        points = int(hit.get("points") or 0)
        if points < min_points:
            continue
        body = hit.get("story_text") or hit.get("comment_text") or ""
        if not body.strip() and not hit.get("url"):
            # Story with no body and no URL → nothing to ingest.
            continue
        body_text = _strip_pii(_strip_html(body)).strip()
        kind = "story" if hit.get("story_text") is not None or hit.get("title") else "comment"
        object_id = hit.get("objectID") or ""
        title = hit.get("title") or f"HN {kind} {object_id}"
        link = f"https://news.ycombinator.com/item?id={object_id}"
        if not body_text:
            # Story-only with external URL — use that URL as the content reference.
            body_text = f"(external link: {hit.get('url', '')})"
        out.append({
            "path": f"hn/{object_id}",
            "content": f"# {title}\n\n{body_text}",
            "source_type": "hn_comment",
            "source_url": link,
            "source_ref": str(object_id),
            "quality_signal": {
                "points": points,
                "num_comments": int(hit.get("num_comments") or 0),
                "kind": kind,
                "created_at": hit.get("created_at") or "",
            },
        })
    return out


# ---------------------------------------------------------------------------
# arXiv (Atom XML)
# ---------------------------------------------------------------------------

_ATOM_NS = {"atom": "http://www.w3.org/2005/Atom"}


def _parse_arxiv_atom(xml_text: str, limit: int) -> list[dict[str, Any]]:
    """Parse arXiv Atom-feed XML → list of entry dicts."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        logger.warning("arxiv_atom_parse_failed: %s", exc)
        return []
    out: list[dict[str, Any]] = []
    for entry in root.findall("atom:entry", _ATOM_NS)[:limit]:
        title = (entry.findtext("atom:title", default="", namespaces=_ATOM_NS) or "").strip()
        summary = (entry.findtext("atom:summary", default="", namespaces=_ATOM_NS) or "").strip()
        if not summary:
            continue
        arxiv_url = (entry.findtext("atom:id", default="", namespaces=_ATOM_NS) or "").strip()
        # ID looks like http://arxiv.org/abs/2310.06825v1
        arxiv_id = arxiv_url.rsplit("/", 1)[-1] if arxiv_url else ""
        authors = [
            (a.findtext("atom:name", default="", namespaces=_ATOM_NS) or "").strip()
            for a in entry.findall("atom:author", _ATOM_NS)
        ]
        author_str = ", ".join(a for a in authors if a) or "?"
        published = (entry.findtext("atom:published", default="", namespaces=_ATOM_NS) or "").strip()
        content = (
            f"# {title}\n\n_Authors:_ {author_str}\n\n## Abstract\n{summary}"
        )
        out.append({
            "path": f"arxiv/{arxiv_id}",
            "content": content,
            "source_type": "paper_abstract",
            "source_url": arxiv_url,
            "source_ref": arxiv_id,
            "quality_signal": {
                "published": published,
                "author_count": len(authors),
            },
        })
    return out


async def fetch_arxiv(mode: str, value: str, limit: int) -> list[dict[str, Any]]:
    """Fetch from arXiv API.

    - ``mode="id"`` — single-paper lookup via ``?id_list=<value>``.
    - ``mode="query"`` — search via ``?search_query=all:<value>``,
      capped at ``limit``.

    ID lookups cache the Atom body with immutable TTL (papers don't
    change post-publication). Search responses cache with short TTL
    (results drift as new papers index).
    """
    if limit <= 0:
        return []
    client = get_generic_http_client()
    cache = get_fetch_cache()

    if mode == "id":
        cached = await cache.get("arxiv", value, "atom")
        if cached:
            return _parse_arxiv_atom(cached.decode("utf-8", errors="replace"), limit)
        url = f"{_ARXIV_BASE}?id_list={value}"
    elif mode == "query":
        qhash = _query_hash(value)
        cached = await cache.get("arxiv", f"q-{qhash}", "atom")
        if cached:
            return _parse_arxiv_atom(cached.decode("utf-8", errors="replace"), limit)
        params = httpx.QueryParams({
            "search_query": f"all:{value}",
            "max_results": str(min(limit, 50)),
            "sortBy": "relevance",
            "sortOrder": "descending",
        })
        url = f"{_ARXIV_BASE}?{params}"
    else:
        raise ValueError(f"arxiv mode must be 'id' or 'query', got {mode!r}")

    r = await client.get(url)
    r.raise_for_status()
    body = r.content
    cache_key = (value if mode == "id" else f"q-{_query_hash(value)}")
    ttl = (
        settings.fetch_cache_ttl_immutable_seconds
        if mode == "id"
        else settings.fetch_cache_ttl_default_seconds
    )
    try:
        await cache.put("arxiv", cache_key, "atom", body, ttl_seconds=ttl)
    except Exception as exc:
        logger.debug("arxiv_cache_put_failed: %s", exc)

    return _parse_arxiv_atom(body.decode("utf-8", errors="replace"), limit)


# ---------------------------------------------------------------------------
# Reddit (allowlisted)
# ---------------------------------------------------------------------------

async def fetch_reddit_posts(
    subreddit: str, query: str, limit: int, min_score: int, min_comments: int,
) -> list[dict[str, Any]]:
    """Search a single allowlisted subreddit for top-scoring text posts.

    Allowlist enforcement is the caller's job (``_parse_reddit_ref`` does it
    at parse time). This function trusts ``subreddit``.

    Quality gates: ``score >= min_score`` AND ``num_comments >= min_comments``
    AND ``over_18=False``. Text posts only (``selftext`` non-empty) — link
    posts have no body to ingest. Bodies PII-stripped before storage.

    Search response cached by ``(subreddit, query, gates)`` with the short
    TTL — Reddit search rankings drift as posts age.
    """
    if limit <= 0:
        return []
    client = get_generic_http_client()
    cache = get_fetch_cache()

    gate_key = f"q-{_query_hash(query)}-s{min_score}-c{min_comments}"
    cached = await cache.get("reddit", f"sub-{subreddit.lower()}", gate_key)
    if cached:
        try:
            data = json.loads(cached)
        except Exception:
            data = None
    else:
        data = None

    if data is None:
        params = httpx.QueryParams({
            "q": query,
            "restrict_sr": "on",
            "sort": "top",
            "t": "all",
            "limit": str(min(limit * 2, 100)),
        })
        url = f"{_REDDIT_BASE}/r/{subreddit}/search.json?{params}"
        r = await client.get(url)
        if r.status_code == 429:
            logger.warning("reddit_rate_limited: subreddit=%s", subreddit)
            return []
        if r.status_code == 404:
            logger.warning("reddit_subreddit_404: %s", subreddit)
            return []
        r.raise_for_status()
        data = r.json()
        try:
            await cache.put(
                "reddit", f"sub-{subreddit.lower()}", gate_key,
                json.dumps(data).encode("utf-8"),
                ttl_seconds=settings.fetch_cache_ttl_default_seconds,
            )
        except Exception as exc:
            logger.debug("reddit_cache_put_failed: %s", exc)

    children = ((data or {}).get("data") or {}).get("children") or []
    out: list[dict[str, Any]] = []
    for child in children:
        if len(out) >= limit:
            break
        post = (child or {}).get("data") or {}
        if post.get("over_18"):
            continue
        score = int(post.get("score") or 0)
        ncomments = int(post.get("num_comments") or 0)
        if score < min_score or ncomments < min_comments:
            continue
        body = (post.get("selftext") or "").strip()
        if not body:
            continue  # link-only post, no ingestible content
        title = post.get("title") or ""
        post_id = post.get("id") or ""
        permalink = post.get("permalink") or ""
        body_text = _strip_pii(body)
        out.append({
            "path": f"reddit/{subreddit}/{post_id}",
            "content": f"# r/{subreddit}: {title}\n\n{body_text}",
            "source_type": "reddit_post",
            "source_url": f"{_REDDIT_BASE}{permalink}" if permalink else "",
            "source_ref": f"t3_{post_id}",
            "quality_signal": {
                "score": score,
                "num_comments": ncomments,
                "subreddit": subreddit,
                "created_utc": post.get("created_utc"),
            },
        })
    return out


# ---------------------------------------------------------------------------
# Wikipedia
# ---------------------------------------------------------------------------

async def fetch_wiki_pages(query: str, limit: int) -> list[dict[str, Any]]:
    """Search English Wikipedia for ``query`` and ingest the top pages.

    Two-step:
    1. ``?action=query&list=search&srsearch=<q>`` → list of matching page titles.
    2. ``?action=query&prop=extracts|info&explaintext=true&titles=<batch>`` →
       plain-text extracts + the current ``lastrevid`` (used as source_ref).

    Each entry tagged ``source_type=wiki_article``. Search response cached
    by query hash with the short TTL; revisions can land at any time.
    """
    if limit <= 0:
        return []
    client = get_generic_http_client()
    cache = get_fetch_cache()

    qhash = _query_hash(query)
    cached = await cache.get("wiki", f"search-{qhash}", f"top{limit}")
    titles: list[str] = []
    if cached:
        try:
            titles = json.loads(cached)
        except Exception:
            titles = []

    if not titles:
        search_params = httpx.QueryParams({
            "action": "query",
            "list": "search",
            "srsearch": query,
            "srlimit": str(min(limit, 50)),
            "format": "json",
            "formatversion": "2",
        })
        r = await client.get(f"{_WIKI_BASE}?{search_params}")
        r.raise_for_status()
        results = ((r.json() or {}).get("query") or {}).get("search") or []
        titles = [hit["title"] for hit in results if hit.get("title")]
        try:
            await cache.put(
                "wiki", f"search-{qhash}", f"top{limit}",
                json.dumps(titles).encode("utf-8"),
                ttl_seconds=settings.fetch_cache_ttl_default_seconds,
            )
        except Exception as exc:
            logger.debug("wiki_search_cache_put_failed: %s", exc)

    if not titles:
        return []

    titles_param = "|".join(titles[:limit])
    extract_params = httpx.QueryParams({
        "action": "query",
        "prop": "extracts|info",
        "explaintext": "true",
        "titles": titles_param,
        "format": "json",
        "formatversion": "2",
    })
    r = await client.get(f"{_WIKI_BASE}?{extract_params}")
    r.raise_for_status()
    pages = ((r.json() or {}).get("query") or {}).get("pages") or []
    out: list[dict[str, Any]] = []
    for p in pages:
        title = p.get("title") or ""
        extract = (p.get("extract") or "").strip()
        if not extract:
            continue
        revid = str(p.get("lastrevid") or "")
        # URL-safe title (Wikipedia uses underscores for spaces in URLs)
        url_title = title.replace(" ", "_")
        out.append({
            "path": f"wiki/{title}",
            "content": f"# {title}\n\n{extract}",
            "source_type": "wiki_article",
            "source_url": f"https://en.wikipedia.org/wiki/{url_title}",
            "source_ref": revid or title,
            "quality_signal": {
                "lastrevid": revid,
                "page_length": len(extract),
            },
        })
    return out


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

async def fetch_forum(prefix: str, value: str) -> list[dict[str, Any]]:
    """prefix ∈ {so, hn, arxiv}; value is the parsed query/id."""
    if prefix == "so":
        return await fetch_so_answers(
            value, settings.so_max_answers, settings.so_min_score,
        )
    if prefix == "hn":
        return await fetch_hn_items(
            value, settings.hn_max_items, settings.hn_min_points,
        )
    if prefix == "arxiv":
        # Caller passes the raw value; we detect id vs query here for the
        # dispatch wrapper's convenience. The research-agent path already
        # routes via _parse_arxiv_ref so this branch is for direct callers.
        from app.modules.research_extractors import _parse_arxiv_ref
        mode, val = _parse_arxiv_ref(f"arxiv:{value}")
        return await fetch_arxiv(mode, val, settings.arxiv_max_sections)
    if prefix == "reddit":
        # value packs `<subreddit>:<query>`
        sub, q = value.split(":", 1)
        return await fetch_reddit_posts(
            sub, q, settings.reddit_max_posts,
            settings.reddit_min_score, settings.reddit_min_comments,
        )
    if prefix == "wiki":
        return await fetch_wiki_pages(value, settings.wiki_max_pages)
    raise ValueError(f"Unknown forum prefix: {prefix!r}")
