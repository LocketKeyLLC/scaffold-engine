"""GitHub repo ingestion for /research github:owner/repo[@<ref>].

Fetches README, docs/**/*.md, top-level Python module docstrings,
tests/*.py docstrings, .github/workflows/*.yml, release notes, and
closed-issue/merged-PR threads with a reaction-count quality gate.

Each returned entry carries ``source_type``, ``source_url``,
``source_ref`` (SHA or branch name), and ``quality_signal`` — callers
pass these straight to ``ingest_entries`` for provenance recording.
"""
import asyncio
import ast
import base64
import json
import logging
from typing import Any

import httpx
import redis.asyncio as aioredis

from app.config import settings
from app.utils.fetch_cache import get_fetch_cache
from app.utils.http_clients import get_github_client

logger = logging.getLogger(__name__)

_DOCS_PREFIX = "docs/"
_TESTS_PREFIXES = ("tests/", "test/", "spec/")
_CI_WORKFLOWS_PREFIX = ".github/workflows/"
_RELEASE_NOTES_BASENAMES = frozenset({
    "changelog.md", "releases.md", "history.md", "changes.md", "news.md",
})

# Audit M6 — versioned cache key prefix for the tree response cache. Bumping
# the version invalidates every cached entry (use when the cached payload
# shape changes, not when GitHub data changes — that's what ETags are for).
_GITHUB_TREE_CACHE_KEY_PREFIX = "github:tree:v1"

_redis: aioredis.Redis | None = None


async def _redis_client() -> aioredis.Redis | None:
    """Lazy-init Redis client for the tree cache.

    Returns None if the cache is disabled (TTL=0) or if the connection
    init fails. All callers fail-open: a None client means "skip the
    cache, do a live API call."
    """
    global _redis
    if settings.github_tree_cache_ttl_seconds <= 0:
        return None
    if _redis is None:
        try:
            _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
        except Exception as exc:
            logger.warning("github_ingest: redis init failed, cache disabled: %s", exc)
            return None
    return _redis


def _tree_cache_key(owner: str, repo: str, branch: str) -> str:
    return f"{_GITHUB_TREE_CACHE_KEY_PREFIX}:{owner}/{repo}:{branch}"


async def _read_cached_tree(
    owner: str, repo: str, branch: str,
) -> tuple[str, list[dict], bool] | None:
    """Return (etag, blobs, truncated) from cache, or None on miss/error/disabled."""
    r = await _redis_client()
    if r is None:
        return None
    try:
        raw = await r.get(_tree_cache_key(owner, repo, branch))
        if not raw:
            return None
        entry = json.loads(raw)
        return entry["etag"], entry["blobs"], entry["truncated"]
    except Exception as exc:
        logger.debug("github_ingest: tree cache read failed: %s", exc)
        return None


async def _write_cached_tree(
    owner: str, repo: str, branch: str,
    etag: str, blobs: list[dict], truncated: bool,
) -> None:
    r = await _redis_client()
    if r is None:
        return
    try:
        payload = json.dumps({"etag": etag, "blobs": blobs, "truncated": truncated})
        await r.set(
            _tree_cache_key(owner, repo, branch),
            payload,
            ex=settings.github_tree_cache_ttl_seconds,
        )
    except Exception as exc:
        logger.debug("github_ingest: tree cache write failed: %s", exc)


async def _refresh_cached_tree_ttl(owner: str, repo: str, branch: str) -> None:
    """Touch an existing entry's TTL on a 304 hit — keeps hot entries hot."""
    r = await _redis_client()
    if r is None:
        return
    try:
        await r.expire(
            _tree_cache_key(owner, repo, branch),
            settings.github_tree_cache_ttl_seconds,
        )
    except Exception as exc:
        logger.debug("github_ingest: tree cache TTL refresh failed: %s", exc)


class GitHubRepoNotFoundError(Exception):
    """Repo inaccessible (404 — wrong owner/repo, or private without token)."""


class GitHubRateLimitError(Exception):
    """Rate limit exhausted."""


def check_github_rate_limit(response: httpx.Response) -> None:
    """Inspect GitHub response headers and raise if rate limit exhausted.

    Shared by github_ingest and gt_extractor (push path). Also upgrades an
    HTTP 429 response into a GitHubRateLimitError so callers get one consistent
    exception type for all rate-limit situations (#70).
    """
    if response.status_code == 429:
        reset = response.headers.get("X-RateLimit-Reset", "unknown")
        retry_after = response.headers.get("Retry-After", "unknown")
        raise GitHubRateLimitError(
            f"GitHub returned 429. Reset={reset} Retry-After={retry_after}"
        )
    remaining = response.headers.get("X-RateLimit-Remaining")
    if remaining is None:
        return
    remaining_int = int(remaining)
    if remaining_int == 0:
        reset = response.headers.get("X-RateLimit-Reset", "unknown")
        raise GitHubRateLimitError(f"GitHub rate limit exhausted. Resets at {reset}")
    if remaining_int < 10:
        logger.warning("GitHub rate limit low: %d remaining", remaining_int)


# Backward-compat alias for internal callers
_check_rate_limit = check_github_rate_limit


async def _get_default_branch(client: httpx.AsyncClient, owner: str, repo: str) -> str:
    r = await client.get(f"/repos/{owner}/{repo}")
    _check_rate_limit(r)
    if r.status_code == 404:
        raise GitHubRepoNotFoundError(f"{owner}/{repo} not found or inaccessible")
    r.raise_for_status()
    return r.json().get("default_branch", "main")


async def _resolve_ref_to_sha(
    client: httpx.AsyncClient, owner: str, repo: str, ref: str,
) -> str:
    """Resolve a tag/branch/SHA to its commit SHA.

    ``GET /repos/{o}/{r}/commits/{ref}`` accepts all three. Raises
    ``GitHubRepoNotFoundError`` on 404 (unknown ref).
    """
    r = await client.get(f"/repos/{owner}/{repo}/commits/{ref}")
    _check_rate_limit(r)
    if r.status_code == 404:
        raise GitHubRepoNotFoundError(f"{owner}/{repo}@{ref} not found")
    r.raise_for_status()
    return r.json().get("sha", "")


async def _fetch_readme(client: httpx.AsyncClient, owner: str, repo: str) -> tuple[str, str]:
    """Returns (path, content) or ('', '') if no README."""
    r = await client.get(f"/repos/{owner}/{repo}/readme")
    _check_rate_limit(r)
    if r.status_code == 404:
        return "", ""
    r.raise_for_status()
    data = r.json()
    try:
        content = base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except (ValueError, TypeError, KeyError) as e:
        # Distinguish decode failure from "no README" — caller logs and continues,
        # but does not silently treat a corrupt README as missing.
        logger.error("README decode failed for %s/%s: %s", owner, repo, e)
        raise
    return data.get("path", "README"), content


async def _get_tree(client: httpx.AsyncClient, owner: str, repo: str, branch: str) -> tuple[list[dict], bool]:
    """Fetch repo tree, with optional Redis cache backed by GitHub ETag.

    Audit M6 (closes #151). Cache stores ``(etag, blobs, truncated)`` per
    ``(owner, repo, branch)``. On hit we send ``If-None-Match: <etag>``;
    GitHub returns 304 (free — doesn't count against rate limit) when the
    tree hasn't changed, and we return the cached blobs unchanged. On 200
    we re-parse and refresh the cache. Cache failures fail-open to a
    normal live call.
    """
    cached = await _read_cached_tree(owner, repo, branch)
    headers = {"If-None-Match": cached[0]} if cached is not None else None

    r = await client.get(
        f"/repos/{owner}/{repo}/git/trees/{branch}",
        params={"recursive": "1"},
        headers=headers,
    )
    _check_rate_limit(r)

    if r.status_code == 304 and cached is not None:
        # GitHub confirms unchanged — return cached payload, refresh TTL.
        _, cached_blobs, cached_truncated = cached
        await _refresh_cached_tree_ttl(owner, repo, branch)
        return cached_blobs, cached_truncated

    r.raise_for_status()
    data = r.json()
    truncated = bool(data.get("truncated"))
    if truncated:
        # #68 — escalate to ERROR so log alerts notice; the SSE layer can
        # surface the _truncated marker to the user via fetch_repo_content.
        logger.error(
            "GitHub tree truncated for %s/%s — results are INCOMPLETE (repo exceeds API tree cap)",
            owner, repo,
        )
    blobs = [e for e in data.get("tree", []) if e.get("type") == "blob"]

    etag = r.headers.get("etag")
    if etag:
        await _write_cached_tree(owner, repo, branch, etag, blobs, truncated)
    # Return (blobs, truncated) — caller decides whether to surface/raise
    return blobs, truncated


def _classify_path(path: str) -> str:
    """Map a repo path → source_type.

    Heuristic order matters: ``CHANGELOG.md`` in ``docs/`` should still be
    tagged ``release_notes``, not ``tech_docs``.
    """
    p = path.lower()
    basename = path.rsplit("/", 1)[-1].lower()
    if basename in _RELEASE_NOTES_BASENAMES:
        return "release_notes"
    if p.startswith(_CI_WORKFLOWS_PREFIX) and (p.endswith(".yml") or p.endswith(".yaml")):
        return "ci_config"
    if p.endswith(".py") and any(p.startswith(pre) for pre in _TESTS_PREFIXES):
        return "test_code"
    return "tech_docs"


def _select_tree_files(tree: list[dict], remaining_cap: int) -> list[dict]:
    """Pick docs/**/*.md, top-level *.md, top-level *.py, one-level tests/*.py,
    and .github/workflows/*.yml — capped.

    Top-level .py is the historic root-docstring path. tests/*.py (one level
    deep only) extracts test-module docstrings — full test bodies are too
    noisy for the embedding model. CI workflows go in as raw YAML.
    """
    docs = [
        e for e in tree
        if e["path"].endswith(".md")
        and (e["path"].startswith(_DOCS_PREFIX) or "/" not in e["path"])
    ]
    pyfiles = [e for e in tree if e["path"].endswith(".py") and "/" not in e["path"]]
    test_py = [
        e for e in tree
        if e["path"].endswith(".py")
        and any(e["path"].startswith(pre) for pre in _TESTS_PREFIXES)
        and e["path"].count("/") == 1  # one level deep only
    ]
    ci_yaml = [
        e for e in tree
        if e["path"].startswith(_CI_WORKFLOWS_PREFIX)
        and (e["path"].endswith(".yml") or e["path"].endswith(".yaml"))
    ]
    selected = docs + pyfiles + test_py + ci_yaml
    if len(selected) > remaining_cap:
        logger.warning("File count %d exceeds remaining cap %d — truncating", len(selected), remaining_cap)
        selected = selected[:remaining_cap]
    return selected


async def _fetch_blob(client: httpx.AsyncClient, owner: str, repo: str, sha: str) -> str:
    r = await client.get(f"/repos/{owner}/{repo}/git/blobs/{sha}")
    _check_rate_limit(r)
    r.raise_for_status()
    data = r.json()
    if data.get("encoding") != "base64":
        return ""
    try:
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")
    except Exception as e:
        logger.warning("Blob decode failed: %s", e)
        return ""


def _extract_docstring(source: str) -> str:
    # #71 — widen catch: malformed sources can raise ValueError (null bytes)
    # or TypeError (bad ast input) in addition to SyntaxError
    try:
        return ast.get_docstring(ast.parse(source)) or ""
    except (SyntaxError, ValueError, TypeError) as e:
        logger.debug("docstring extract failed: %s", e)
        return ""


async def fetch_repo_content(
    owner: str, repo: str, ref_hint: str | None = None,
) -> list[dict[str, Any]]:
    """Fetch ingestible content from a GitHub repo at a ref.

    ``ref_hint=None`` → use the repo's default branch (back-compat path:
    branch name lands in each entry's ``source_ref``, weakly immutable).
    ``ref_hint=<tag|branch|sha>`` → resolve to commit SHA via
    ``/repos/{o}/{r}/commits/{ref}`` and use the SHA as the tree ref and
    in each entry's ``source_ref`` (strongly immutable — producers get
    "this content lived at this SHA at fetch time" guarantee).

    Returns list of entry dicts with: path, content, source_type,
    source_url, source_ref, quality_signal. ``source_type`` is classified
    per filename: CHANGELOG.md → ``release_notes``, tests/*.py →
    ``test_code``, .github/workflows/*.yml → ``ci_config``, rest → ``tech_docs``.

    Raises:
        GitHubRepoNotFoundError: repo or ref 404
        GitHubRateLimitError: rate limit exhausted
    """
    client = get_github_client()

    if ref_hint is None:
        # Back-compat path: branch name, no SHA pinning. Existing call
        # sequence (default_branch → readme → tree) preserved.
        branch = await _get_default_branch(client, owner, repo)
        source_ref = branch
        tree_ref = branch
    else:
        # Explicit pin: resolve to commit SHA for immutable provenance.
        source_ref = await _resolve_ref_to_sha(client, owner, repo, ref_hint)
        if not source_ref:
            raise GitHubRepoNotFoundError(f"{owner}/{repo}@{ref_hint} sha unresolved")
        tree_ref = source_ref

    results: list[dict[str, Any]] = []

    # 1. README (dedicated endpoint — handles case/extension automatically)
    readme_path, readme_content = await _fetch_readme(client, owner, repo)
    if readme_content.strip():
        results.append({
            "path": readme_path,
            "content": readme_content,
            "source_type": _classify_path(readme_path),
            "source_url": f"https://github.com/{owner}/{repo}/blob/{tree_ref}/{readme_path}",
            "source_ref": source_ref,
            "quality_signal": {},
        })
    elif readme_path:  # README endpoint returned something but body is empty/whitespace
        logger.warning(
            "GitHub README is empty/whitespace-only, dropping: %s/%s path=%s",
            owner, repo, readme_path,
        )

    # 2. Tree walk (docs/**/*.md + top-level *.py + tests/*.py + .github/workflows/*.yml)
    tree_truncated = False
    attempted = 0
    remaining = settings.github_max_files - len(results)
    if remaining > 0:
        tree, tree_truncated = await _get_tree(client, owner, repo, tree_ref)
        selected = _select_tree_files(tree, remaining)
        attempted = len(selected)

        # #69 — parallelize blob fetches. Semaphore bounds concurrent GitHub
        # calls so we don't blow through the rate limit in bursts.
        sem = asyncio.Semaphore(settings.github_blob_concurrency)

        async def _fetch_one(entry: dict) -> dict | None:
            path = entry["path"]
            async with sem:
                content = await _fetch_blob(client, owner, repo, entry["sha"])
            if not content.strip():
                return None
            stype = _classify_path(path)
            # *.py paths get docstring-only treatment: top-level for
            # tech_docs (existing behavior), per-module for test_code
            # (full bodies are too noisy for the embedder).
            if path.endswith(".py"):
                docstring = _extract_docstring(content)
                if not docstring:
                    return None
                content = docstring
            return {
                "path": path,
                "content": content,
                "source_type": stype,
                "source_url": f"https://github.com/{owner}/{repo}/blob/{tree_ref}/{path}",
                "source_ref": source_ref,
                "quality_signal": {},
            }

        fetched = await asyncio.gather(
            *(_fetch_one(e) for e in selected), return_exceptions=True,
        )
        for item in fetched:
            # CancelledError is a BaseException (Py3.8+), not Exception, so
            # the broader catch below does NOT cover it; without this branch
            # a cancelled child slips through as a non-dict result and
            # crashes downstream consumers. Re-raise so cancellation
            # propagates to the caller's await.
            if isinstance(item, asyncio.CancelledError):
                raise item
            if isinstance(item, (GitHubRateLimitError, GitHubRepoNotFoundError)):
                # Critical errors must NOT be swallowed — propagate so caller
                # sees the real failure mode instead of a silent partial result.
                raise item
            if isinstance(item, BaseException):
                # Any other exception (including unexpected BaseException
                # subclasses) is logged and skipped — the task that raised
                # is dropped from results but the rest of the fetch succeeds.
                logger.warning("Blob fetch failed (transient): %s", item)
                continue
            if item is not None:
                results.append(item)

    logger.info(
        "GitHub fetch: %s/%s ref=%s attempted=%d files=%d tree_truncated=%s",
        owner, repo, source_ref[:12], attempted, len(results), tree_truncated,
    )
    return results


async def fetch_repo_releases(
    owner: str, repo: str, limit: int,
) -> list[dict[str, Any]]:
    """Fetch up to ``limit`` release-note entries, newest first.

    Each entry tagged ``source_type=release_notes`` with the release's
    ``tag_name`` as the ``source_ref``. Drafts and bodyless releases skipped.

    Response cached under ``fetchv1:gh:list-latest:releases-…`` with the
    short TTL — release LIST grows as new versions ship, so the cache is
    a within-session dedup, not a long-term store. Individual release
    bodies for tagged versions are immutable; today they ride along
    inside the list response.
    """
    if limit <= 0:
        return []
    cache = get_fetch_cache()
    cache_path = f"releases:{owner}/{repo}:limit-{min(limit, 100)}"

    releases_json: list | None = None
    cached = await cache.get("gh", "list-latest", cache_path)
    if cached:
        try:
            releases_json = json.loads(cached)
        except Exception as exc:
            logger.debug("gh_releases_cache_decode_failed: %s", exc)

    if releases_json is None:
        client = get_github_client()
        r = await client.get(
            f"/repos/{owner}/{repo}/releases",
            params={"per_page": min(limit, 100)},
        )
        _check_rate_limit(r)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        releases_json = r.json()
        try:
            await cache.put(
                "gh", "list-latest", cache_path,
                json.dumps(releases_json).encode("utf-8"),
                ttl_seconds=settings.fetch_cache_ttl_default_seconds,
            )
        except Exception as exc:
            logger.debug("gh_releases_cache_put_failed: %s", exc)

    out: list[dict[str, Any]] = []
    for rel in releases_json[:limit]:
        if rel.get("draft"):
            continue
        body = (rel.get("body") or "").strip()
        if not body:
            continue
        tag = rel.get("tag_name", "")
        title = rel.get("name") or tag or "release"
        out.append({
            "path": f"release/{tag}",
            "content": f"# {title}\n\n{body}",
            "source_type": "release_notes",
            "source_url": rel.get("html_url", ""),
            "source_ref": tag,
            "quality_signal": {
                "prerelease": bool(rel.get("prerelease")),
                "published_at": rel.get("published_at") or "",
            },
        })
    return out


_DISCUSSIONS_GRAPHQL_QUERY = """
query($owner: String!, $repo: String!, $first: Int!) {
  repository(owner: $owner, name: $repo) {
    discussions(first: $first, orderBy: {field: UPDATED_AT, direction: DESC}, answered: true) {
      nodes {
        number
        title
        body
        url
        upvoteCount
        category { name }
        answer {
          body
        }
      }
    }
  }
}
"""


async def fetch_repo_discussions(
    owner: str, repo: str, limit: int,
) -> list[dict[str, Any]]:
    """Fetch answered GitHub Discussions via GraphQL (§17.124).

    GitHub Discussions aren't exposed via REST — GraphQL is the only path.
    Filter ``answered: true`` so we only ingest community-validated threads
    that have a chosen answer. Each kept entry tagged ``source_type=community``,
    ``source_ref=discussion-<number>``.

    Requires ``GITHUB_TOKEN`` — anonymous GraphQL is rejected (401).
    Returns empty list when no token configured (graceful degradation,
    not a hard error: a repo may not have Discussions enabled either).

    Response cached at ``fetchv1:gh:list-latest:discussions-…`` with the
    short TTL — discussion bodies + answers can be edited, so this is
    within-session dedup, not durable storage.
    """
    if limit <= 0:
        return []
    if not settings.github_token:
        logger.info(
            "github_discussions_skipped: GITHUB_TOKEN required for GraphQL"
        )
        return []

    cache = get_fetch_cache()
    cache_path = f"discussions:{owner}/{repo}:answered:limit-{min(limit, 100)}"

    nodes: list | None = None
    cached = await cache.get("gh", "list-latest", cache_path)
    if cached:
        try:
            nodes = json.loads(cached)
        except Exception as exc:
            logger.debug("gh_discussions_cache_decode_failed: %s", exc)

    if nodes is None:
        client = get_github_client()
        try:
            r = await client.post(
                "/graphql",
                json={
                    "query": _DISCUSSIONS_GRAPHQL_QUERY,
                    "variables": {
                        "owner": owner, "repo": repo, "first": min(limit, 100),
                    },
                },
            )
        except Exception as exc:
            logger.warning("gh_discussions_fetch_failed: %s/%s err=%s", owner, repo, exc)
            return []
        _check_rate_limit(r)
        if r.status_code != 200:
            logger.warning(
                "gh_discussions_unexpected_status: %s/%s status=%d",
                owner, repo, r.status_code,
            )
            return []
        body = r.json()
        # GraphQL errors land in body["errors"] with a 200 status. Common
        # cases: repository not found (404 equivalent), Discussions not
        # enabled, or read scope missing on the token. All treated as
        # "no data" rather than raising.
        if body.get("errors"):
            logger.info(
                "gh_discussions_graphql_errors: %s/%s err=%s",
                owner, repo, str(body["errors"])[:200],
            )
            return []
        repo_data = (body.get("data") or {}).get("repository") or {}
        nodes = (repo_data.get("discussions") or {}).get("nodes") or []
        try:
            await cache.put(
                "gh", "list-latest", cache_path,
                json.dumps(nodes).encode("utf-8"),
                ttl_seconds=settings.fetch_cache_ttl_default_seconds,
            )
        except Exception as exc:
            logger.debug("gh_discussions_cache_put_failed: %s", exc)

    out: list[dict[str, Any]] = []
    for d in nodes[:limit]:
        body_text = (d.get("body") or "").strip()
        answer = d.get("answer") or {}
        answer_body = (answer.get("body") or "").strip()
        if not body_text and not answer_body:
            continue
        number = d.get("number")
        title = d.get("title", "")
        content_parts = [f"# Discussion #{number}: {title}"]
        if body_text:
            content_parts.append(body_text)
        if answer_body:
            content_parts.append(f"## Accepted Answer\n\n{answer_body}")
        out.append({
            "path": f"discussion/{number}",
            "content": "\n\n".join(content_parts),
            "source_type": "community",
            "source_url": d.get("url", ""),
            "source_ref": f"discussion-{number}",
            "quality_signal": {
                "upvotes": int(d.get("upvoteCount") or 0),
                "kind": "discussion",
                "category": (d.get("category") or {}).get("name", ""),
                "has_answer": bool(answer_body),
            },
        })
    return out


async def fetch_repo_issues_and_prs(
    owner: str, repo: str, limit: int, min_reactions: int,
) -> list[dict[str, Any]]:
    """Closed issues + merged-or-closed PRs, sorted by reactions, gated.

    The ``/issues`` endpoint returns both issues and PRs (PRs identified
    by ``pull_request`` key). We rank by positive reaction count
    (``+1``, ``heart``, ``hooray``) and drop anything below
    ``min_reactions``. Each kept entry tagged ``source_type=community``.

    PR merge state isn't on the /issues payload — checking would cost a
    second call per PR. The reaction gate is a strong-enough proxy:
    closed-and-thumbs-upped means "people found this valuable" regardless
    of merge.

    Response cached under ``fetchv1:gh:list-latest:issues-…`` with the
    short TTL. Issue bodies + reaction counts can drift (edits, new
    reactions), so this is a within-session dedup, not durable storage.
    """
    if limit <= 0:
        return []
    cache = get_fetch_cache()
    per_page = min(limit * 2, 100)
    cache_path = f"issues:{owner}/{repo}:state-closed:sort-reactions:per_page-{per_page}"

    issues_json: list | None = None
    cached = await cache.get("gh", "list-latest", cache_path)
    if cached:
        try:
            issues_json = json.loads(cached)
        except Exception as exc:
            logger.debug("gh_issues_cache_decode_failed: %s", exc)

    if issues_json is None:
        client = get_github_client()
        r = await client.get(
            f"/repos/{owner}/{repo}/issues",
            params={
                "state": "closed",
                "sort": "reactions-+1",
                "per_page": per_page,
            },
        )
        _check_rate_limit(r)
        if r.status_code == 404:
            return []
        r.raise_for_status()
        issues_json = r.json()
        try:
            await cache.put(
                "gh", "list-latest", cache_path,
                json.dumps(issues_json).encode("utf-8"),
                ttl_seconds=settings.fetch_cache_ttl_default_seconds,
            )
        except Exception as exc:
            logger.debug("gh_issues_cache_put_failed: %s", exc)

    out: list[dict[str, Any]] = []
    for item in issues_json:
        if len(out) >= limit:
            break
        reactions = item.get("reactions") or {}
        positive = (
            int(reactions.get("+1", 0))
            + int(reactions.get("heart", 0))
            + int(reactions.get("hooray", 0))
        )
        if positive < min_reactions:
            continue
        body = (item.get("body") or "").strip()
        if not body:
            continue
        kind = "pr" if "pull_request" in item else "issue"
        number = item.get("number", 0)
        title = item.get("title", "")
        out.append({
            "path": f"{kind}/{number}",
            "content": f"# {kind.upper()} #{number}: {title}\n\n{body}",
            "source_type": "community",
            "source_url": item.get("html_url", ""),
            "source_ref": f"{kind}-{number}",
            "quality_signal": {
                "positive_reactions": positive,
                "kind": kind,
                "closed_at": item.get("closed_at") or "",
            },
        })
    return out
