"""Hugging Face Hub ingestion for ``/research hf:<kind>/<id>``.

Five kinds, each returning a list of entry dicts with ``path``,
``content``, ``source_type``, ``source_url``, ``source_ref``,
``quality_signal`` — the shape the GitHub deep mode (§17.106) already
emits, so the research-agent loop can ingest both uniformly.

| kind     | source_type      | source_ref         |
|----------|------------------|--------------------|
| model    | ``model_card``   | HF commit SHA      |
| dataset  | ``dataset_card`` | HF commit SHA      |
| paper    | ``paper_abstract``| arXiv id          |
| space    | ``tech_docs``    | HF commit SHA      |
| doc      | ``official_docs``| topic (mutable)    |

``hf:doc/<library>/<page>`` (§17.122) scrapes the rendered HTML at
``huggingface.co/docs/<topic>`` with trafilatura — HF doesn't expose
docs through a stable public JSON API. Short cache TTL (default 1 h)
since docs can update between releases; no per-revision pin.

The fetch cache (``app/utils/fetch_cache.py``) is consulted for raw
README/card bodies keyed by the resolved commit SHA — immutable refs
get the long ``fetch_cache_ttl_immutable_seconds`` TTL (default 30 d).
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from app.config import settings
from app.utils.fetch_cache import get_fetch_cache
from app.utils.http_clients import get_huggingface_client

logger = logging.getLogger(__name__)


class HFNotFoundError(Exception):
    """The HF resource (model / dataset / paper / space) doesn't exist or is private."""


class HFRateLimitError(Exception):
    """HF API returned 429 (rare on public reads, but possible under load)."""


def _check_response(r: httpx.Response, ctx: str) -> None:
    if r.status_code == 429:
        raise HFRateLimitError(f"HF rate limit on {ctx} (status=429)")
    if r.status_code == 404:
        raise HFNotFoundError(f"HF resource not found: {ctx}")
    r.raise_for_status()


async def _fetch_raw_file_cached(
    client: httpx.AsyncClient,
    repo_kind: str,           # "models" | "datasets" | "spaces"
    id_: str,
    revision: str,
    path: str,
) -> str:
    """Fetch a raw file from ``/<kind>/<id>/raw/<rev>/<path>`` with cache.

    Returns empty string on 404 (e.g., no README). Other errors propagate.
    Cache key uses the resolved commit SHA so re-fetches at the same SHA
    skip the network entirely.
    """
    cache = get_fetch_cache()
    cached = await cache.get("hf", revision, f"{repo_kind}/{id_}/{path}")
    if cached:
        return cached.decode("utf-8", errors="replace")

    url = f"/{repo_kind}/{id_}/raw/{revision}/{path}"
    r = await client.get(url)
    if r.status_code == 404:
        return ""
    _check_response(r, f"raw {repo_kind}/{id_}@{revision}/{path}")
    body = r.content
    await cache.put(
        "hf", revision, f"{repo_kind}/{id_}/{path}",
        body, ttl_seconds=settings.fetch_cache_ttl_immutable_seconds,
    )
    return body.decode("utf-8", errors="replace")


async def _fetch_api_json_cached(
    client: httpx.AsyncClient,
    endpoint: str,
    *,
    cache_ref: str | None = None,
    cache_ttl: int | None = None,
) -> dict:
    """GET an HF API endpoint and return JSON, optionally cached.

    Caching is opt-in via ``cache_ref`` — only the model/dataset/space
    metadata calls that pin to a known SHA benefit. The first metadata
    call (the one that *resolves* the SHA) can't cache because the ref
    isn't known yet — pass ``cache_ref=None`` there.
    """
    if cache_ref is not None:
        cache = get_fetch_cache()
        cached = await cache.get("hf", cache_ref, f"api{endpoint}")
        if cached:
            return json.loads(cached)
    r = await client.get(endpoint)
    _check_response(r, f"api {endpoint}")
    data = r.json()
    if cache_ref is not None and cache_ttl is not None:
        try:
            body = json.dumps(data).encode("utf-8")
            await get_fetch_cache().put(
                "hf", cache_ref, f"api{endpoint}",
                body, ttl_seconds=cache_ttl,
            )
        except Exception as exc:
            logger.debug("hf_api_cache_put_failed: %s", exc)
    return data


def _hub_url(kind_plural: str, id_: str, revision: str | None = None) -> str:
    base = f"{settings.huggingface_api_base}/{kind_plural}/{id_}"
    if revision and revision != "main":
        return f"{base}/tree/{revision}"
    return base


async def fetch_hf_model(id_: str) -> list[dict[str, Any]]:
    """Fetch a model card + key metadata. id_ format: ``owner/repo`` or ``repo``.

    Returns up to ``settings.hf_max_files`` entries: one for the README
    body (if present) and one for the structured metadata summary.
    """
    if settings.hf_max_files <= 0:
        return []
    client = get_huggingface_client()
    meta = await _fetch_api_json_cached(client, f"/api/models/{id_}")
    revision = meta.get("sha") or "main"
    if revision == "main":
        logger.warning("hf_model_unrevisioned: %s — sha missing, falling back to main", id_)

    out: list[dict[str, Any]] = []

    # 1. README body (the canonical model card content)
    readme = await _fetch_raw_file_cached(client, "models", id_, revision, "README.md")
    if readme.strip():
        out.append({
            "path": f"hf:model/{id_}/README.md",
            "content": readme,
            "source_type": "model_card",
            "source_url": _hub_url("models", id_, revision),
            "source_ref": revision,
            "quality_signal": {
                "downloads": int(meta.get("downloads") or 0),
                "likes": int(meta.get("likes") or 0),
                "tags": meta.get("tags") or [],
            },
        })

    # 2. Structured metadata summary — captures pipeline_tag, library, license,
    #    eval results from cardData.model-index. Useful for "what does this
    #    model do" queries that the prose README may not surface clearly.
    summary_lines = [f"# Model: {id_}"]
    if meta.get("pipeline_tag"):
        summary_lines.append(f"- pipeline_tag: {meta['pipeline_tag']}")
    if meta.get("library_name"):
        summary_lines.append(f"- library_name: {meta['library_name']}")
    card_data = meta.get("cardData") or {}
    if card_data.get("license"):
        summary_lines.append(f"- license: {card_data['license']}")
    if card_data.get("base_model"):
        summary_lines.append(f"- base_model: {card_data['base_model']}")
    model_index = card_data.get("model-index") or []
    if model_index:
        summary_lines.append("- evaluation results (model-index):")
        # Each model-index entry has results[]: {task, dataset, metrics}
        for mi in model_index[:5]:
            for res in (mi.get("results") or [])[:5]:
                task = (res.get("task") or {}).get("type", "?")
                ds = (res.get("dataset") or {}).get("name", "?")
                metrics = res.get("metrics") or []
                metric_str = ", ".join(
                    f"{m.get('type','?')}={m.get('value','?')}"
                    for m in metrics[:3]
                )
                summary_lines.append(f"  - {task} on {ds}: {metric_str}")
    if len(summary_lines) > 1:
        out.append({
            "path": f"hf:model/{id_}/metadata",
            "content": "\n".join(summary_lines),
            "source_type": "model_card",
            "source_url": _hub_url("models", id_, revision),
            "source_ref": revision,
            "quality_signal": {
                "downloads": int(meta.get("downloads") or 0),
                "likes": int(meta.get("likes") or 0),
                "has_eval_results": bool(model_index),
            },
        })

    return out[: settings.hf_max_files]


async def fetch_hf_dataset(id_: str) -> list[dict[str, Any]]:
    """Fetch a dataset card + features schema.

    Mirrors fetch_hf_model but for datasets. Adds a features/splits summary
    when present in the API metadata.
    """
    if settings.hf_max_files <= 0:
        return []
    client = get_huggingface_client()
    meta = await _fetch_api_json_cached(client, f"/api/datasets/{id_}")
    revision = meta.get("sha") or "main"
    if revision == "main":
        logger.warning("hf_dataset_unrevisioned: %s — sha missing", id_)

    out: list[dict[str, Any]] = []

    readme = await _fetch_raw_file_cached(client, "datasets", id_, revision, "README.md")
    if readme.strip():
        out.append({
            "path": f"hf:dataset/{id_}/README.md",
            "content": readme,
            "source_type": "dataset_card",
            "source_url": _hub_url("datasets", id_, revision),
            "source_ref": revision,
            "quality_signal": {
                "downloads": int(meta.get("downloads") or 0),
                "likes": int(meta.get("likes") or 0),
                "tags": meta.get("tags") or [],
            },
        })

    summary_lines = [f"# Dataset: {id_}"]
    card_data = meta.get("cardData") or {}
    if card_data.get("license"):
        summary_lines.append(f"- license: {card_data['license']}")
    if card_data.get("language"):
        summary_lines.append(f"- language: {card_data['language']}")
    if card_data.get("task_categories"):
        summary_lines.append(f"- task_categories: {card_data['task_categories']}")
    if card_data.get("size_categories"):
        summary_lines.append(f"- size_categories: {card_data['size_categories']}")
    if len(summary_lines) > 1:
        out.append({
            "path": f"hf:dataset/{id_}/metadata",
            "content": "\n".join(summary_lines),
            "source_type": "dataset_card",
            "source_url": _hub_url("datasets", id_, revision),
            "source_ref": revision,
            "quality_signal": {
                "downloads": int(meta.get("downloads") or 0),
                "likes": int(meta.get("likes") or 0),
            },
        })

    return out[: settings.hf_max_files]


async def fetch_hf_paper(arxiv_id: str) -> list[dict[str, Any]]:
    """Fetch an HF Papers entry by arXiv id.

    Returns a single entry with the abstract + cross-referenced model
    and dataset IDs surfaced by HF's `/api/papers/{id}` endpoint.
    Immutable post-publication, so confidence stays high (§17.104:
    paper_abstract → 0.85).
    """
    client = get_huggingface_client()
    meta = await _fetch_api_json_cached(
        client,
        f"/api/papers/{arxiv_id}",
        cache_ref=arxiv_id,
        cache_ttl=settings.fetch_cache_ttl_immutable_seconds,
    )

    title = (meta.get("title") or "").strip()
    summary = (meta.get("summary") or "").strip()
    if not summary:
        return []

    authors = meta.get("authors") or []
    author_names = ", ".join(
        a.get("name", "") for a in authors if isinstance(a, dict)
    ) or "?"

    body_lines = [f"# {title or arxiv_id}", f"_Authors:_ {author_names}", "", "## Abstract", summary]

    linked_models = meta.get("models") or []
    if linked_models:
        body_lines.append("\n## Implementations on HF Hub")
        for m in linked_models[:10]:
            mid = m.get("id") or m.get("name") or ""
            if mid:
                body_lines.append(f"- model: {mid}")
    linked_datasets = meta.get("datasets") or []
    if linked_datasets:
        for d in linked_datasets[:10]:
            did = d.get("id") or d.get("name") or ""
            if did:
                body_lines.append(f"- dataset: {did}")

    return [{
        "path": f"hf:paper/{arxiv_id}",
        "content": "\n".join(body_lines),
        "source_type": "paper_abstract",
        "source_url": f"{settings.huggingface_api_base}/papers/{arxiv_id}",
        "source_ref": arxiv_id,
        "quality_signal": {
            "linked_models": len(linked_models),
            "linked_datasets": len(linked_datasets),
            "upvotes": int(meta.get("upvotes") or 0),
            "published_at": meta.get("publishedAt") or "",
        },
    }]


async def fetch_hf_space(id_: str) -> list[dict[str, Any]]:
    """Fetch Space metadata + README. Spaces are runnable demos; we don't
    fetch the app code (high token cost, marginal "ground truth" value).
    """
    if settings.hf_max_files <= 0:
        return []
    client = get_huggingface_client()
    meta = await _fetch_api_json_cached(client, f"/api/spaces/{id_}")
    revision = meta.get("sha") or "main"

    out: list[dict[str, Any]] = []
    readme = await _fetch_raw_file_cached(client, "spaces", id_, revision, "README.md")
    if readme.strip():
        out.append({
            "path": f"hf:space/{id_}/README.md",
            "content": readme,
            "source_type": "tech_docs",
            "source_url": _hub_url("spaces", id_, revision),
            "source_ref": revision,
            "quality_signal": {
                "likes": int(meta.get("likes") or 0),
                "sdk": meta.get("sdk") or "",
                "runtime_stage": (meta.get("runtime") or {}).get("stage") or "",
            },
        })

    summary_lines = [f"# Space: {id_}"]
    card_data = meta.get("cardData") or {}
    if meta.get("sdk"):
        summary_lines.append(f"- sdk: {meta['sdk']}")
    if card_data.get("title"):
        summary_lines.append(f"- title: {card_data['title']}")
    if card_data.get("emoji"):
        summary_lines.append(f"- emoji: {card_data['emoji']}")
    if card_data.get("license"):
        summary_lines.append(f"- license: {card_data['license']}")
    runtime = meta.get("runtime") or {}
    if runtime.get("stage"):
        summary_lines.append(f"- runtime: {runtime['stage']}")
    if len(summary_lines) > 1:
        out.append({
            "path": f"hf:space/{id_}/metadata",
            "content": "\n".join(summary_lines),
            "source_type": "tech_docs",
            "source_url": _hub_url("spaces", id_, revision),
            "source_ref": revision,
            "quality_signal": {
                "likes": int(meta.get("likes") or 0),
                "sdk": meta.get("sdk") or "",
            },
        })

    return out[: settings.hf_max_files]


async def fetch_hf_doc(topic: str) -> list[dict[str, Any]]:
    """Fetch an HF docs page by topic.

    ``topic`` is the post-``/docs/`` path, e.g.,
    ``transformers/installation`` or ``transformers/v4.35.0/en/model_doc/llama``.
    URL: ``{huggingface_api_base}/docs/{topic}``. Fetches the rendered
    HTML and extracts main-content text via trafilatura.

    ``source_type=official_docs``. ``source_ref`` is the topic string
    (HF docs are mutable — there's no per-revision pin like for
    model/dataset cards), so the cache uses the short TTL.

    HF doesn't expose docs through a stable public JSON API; this is
    HTML scraping. Trafilatura handles main-content extraction.
    """
    if settings.hf_max_files <= 0:
        return []
    if not topic or not topic.strip():
        return []
    import asyncio as _aio
    import trafilatura

    url = f"{settings.huggingface_api_base}/docs/{topic}"
    cache = get_fetch_cache()
    cache_path = f"docs/{topic}"

    cached = await cache.get("hf", "docs-latest", cache_path)
    if cached:
        extracted = cached.decode("utf-8", errors="replace")
    else:
        from app.utils.http_clients import get_generic_http_client
        client = get_generic_http_client()
        try:
            r = await client.get(
                url, timeout=float(settings.huggingface_timeout),
                follow_redirects=True,
            )
        except Exception as exc:
            logger.warning("hf_doc_fetch_failed: topic=%s err=%s", topic, exc)
            return []
        if r.status_code == 404:
            return []
        if r.status_code != 200:
            logger.warning(
                "hf_doc_unexpected_status: topic=%s status=%d", topic, r.status_code,
            )
            return []
        html = r.text
        extracted = await _aio.to_thread(
            trafilatura.extract, html,
            output_format="txt", with_metadata=False,
        )
        if not extracted or not extracted.strip():
            logger.warning("hf_doc_extract_empty: topic=%s", topic)
            return []
        try:
            await cache.put(
                "hf", "docs-latest", cache_path,
                extracted.encode("utf-8"),
                ttl_seconds=settings.fetch_cache_ttl_default_seconds,
            )
        except Exception as exc:
            logger.debug("hf_doc_cache_put_failed: %s", exc)

    return [{
        "path": f"hf:doc/{topic}",
        "content": extracted,
        "source_type": "official_docs",
        "source_url": url,
        "source_ref": topic,
        "quality_signal": {},
    }]


async def fetch_hf(kind: str, id_: str) -> list[dict[str, Any]]:
    """Dispatch helper: kind → fetcher. Used by the research agent."""
    if kind == "model":
        return await fetch_hf_model(id_)
    if kind == "dataset":
        return await fetch_hf_dataset(id_)
    if kind == "paper":
        return await fetch_hf_paper(id_)
    if kind == "space":
        return await fetch_hf_space(id_)
    if kind == "doc":
        return await fetch_hf_doc(id_)
    raise ValueError(f"Unknown HF kind: {kind!r}")
