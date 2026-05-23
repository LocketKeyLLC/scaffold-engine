"""Tests for scripts/reindex.py — Sprint Item 7.

Mocks the Milvus collection (.query / .upsert) and the embed_fn so no
live Milvus / Ollama / OpenAI is required. Verifies the embedding-text
shape mirrors rag_pipeline._build_embedding_text and that upsert payloads
preserve every non-vector field.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# scripts/ isn't a package; load reindex directly so we can call the
# async entry points without spawning the CLI shell.
_REINDEX_PATH = os.path.join(
    os.path.dirname(__file__), "..", "scripts", "reindex.py",
)
_spec = importlib.util.spec_from_file_location("reindex_mod", _REINDEX_PATH)
reindex_mod = importlib.util.module_from_spec(_spec)
sys.modules["reindex_mod"] = reindex_mod
_spec.loader.exec_module(reindex_mod)


# ---------------------------------------------------------------------------
# Helpers — fake Milvus collection that returns a fixed page set.
# ---------------------------------------------------------------------------
def _make_entry(entry_id: str, *, domain: str = "eng",
                title: str = "T", text: str = "body",
                tags: list[str] | None = None,
                **overrides) -> dict[str, Any]:
    # Use ``is None`` rather than truthiness so callers can pass an empty
    # list to mean "no tags" (the truthy-or pattern would silently replace
    # ``[]`` with the default).
    base = {
        "entry_id": entry_id,
        "title": title,
        "canonical_text": text,
        "domain": domain,
        "domain_tags": tags if tags is not None else ["alpha"],
        "confidence_score": 0.8,
        "source_type": "tech_docs",
        "source_url": "https://x.test",
        "content_hash": "h" + entry_id,
        "model_id": "old-embedder",
        "version": 1,
        "supersedes_id": "",
        "created_at": 1000,
        "updated_at": 1000,
        "expires_at": 9_999_999_999,
    }
    base.update(overrides)
    return base


class _FakeCollection:
    """Sequence-of-pages fake. Each ``query()`` call peels the next page off
    ``self.pages``; an empty page terminates the cursor loop."""

    def __init__(self, pages_by_domain: dict[str, list[list[dict]]]):
        self._pages = {d: list(p) for d, p in pages_by_domain.items()}
        self.upsert_calls: list[dict] = []

    def query(self, *, expr: str, output_fields=None, limit=None):
        # Pull the domain out of the expression so we can return the right pages.
        domain = None
        for d in self._pages:
            if f'domain == "{d}"' in expr:
                domain = d
                break
        if domain is None:
            return []
        bucket = self._pages.get(domain) or []
        if not bucket:
            return []
        return bucket.pop(0)

    def upsert(self, rows):
        for row in rows:
            self.upsert_calls.append(row)


# ---------------------------------------------------------------------------
# Embedding text — must match rag_pipeline._build_embedding_text byte for byte.
# ---------------------------------------------------------------------------
def test_embedding_text_matches_rag_pipeline_format():
    """If this drifts, re-embedded vectors won't match query-time embeddings.
    Locking the format here protects against a silent-correctness bug."""
    from app.modules import rag_pipeline as rp

    entry = {
        "title": "RAG basics",
        "domain_tags": ["llm", "rag"],
        "canonical_text": "Body content goes here.",
    }
    assert reindex_mod._build_embedding_text(entry) == rp._build_embedding_text(entry)


def test_embedding_text_handles_missing_fields():
    """An entry with only canonical_text shouldn't crash — title and tags
    are optional fields in toon_v2."""
    assert reindex_mod._build_embedding_text({"canonical_text": "just body"}) == "just body"
    assert reindex_mod._build_embedding_text({"title": "only title"}) == "only title"


# ---------------------------------------------------------------------------
# reindex_partition — core algorithm
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_dry_run_makes_no_upserts():
    """Dry-run must not mutate Milvus — upsert_fn should never fire."""
    col = _FakeCollection({"eng": [[_make_entry("e1"), _make_entry("e2")], []]})
    embed = AsyncMock(return_value=[[0.1] * 512, [0.2] * 512])
    upsert = AsyncMock()

    stats = await reindex_mod.reindex_partition(
        col, "eng",
        new_embedder=None, new_provider=None,
        batch_size=10, dry_run=True, now_ms=1234,
        embed_fn=embed, upsert_fn=upsert,
    )
    assert stats["scanned"] == 2
    assert stats["reembedded"] == 0
    assert embed.call_count == 0
    assert upsert.call_count == 0


@pytest.mark.asyncio
async def test_live_run_reembeds_and_upserts_each_entry():
    col = _FakeCollection({"eng": [
        [_make_entry("e1", title="A", text="body A")],
        [_make_entry("e2", title="B", text="body B")],
        [],
    ]})
    embed = AsyncMock(side_effect=[[[0.5] * 512], [[0.7] * 512]])
    upsert_calls: list[dict] = []

    async def _upsert(row):
        upsert_calls.append(row)

    stats = await reindex_mod.reindex_partition(
        col, "eng",
        new_embedder="new-embedder",
        new_provider=None,
        batch_size=1, dry_run=False, now_ms=5555,
        embed_fn=embed, upsert_fn=_upsert,
    )
    assert stats == {"scanned": 2, "reembedded": 2, "skipped_empty": 0, "errors": 0}
    assert embed.call_count == 2
    assert len(upsert_calls) == 2
    # Each upsert preserves every original field with three exceptions:
    # dense_vector (new), model_id (new-embedder), updated_at (now_ms).
    first = upsert_calls[0]
    assert first["entry_id"] == "e1"
    assert first["title"] == "A"
    assert first["canonical_text"] == "body A"
    assert first["model_id"] == "new-embedder"
    assert first["updated_at"] == 5555
    # truncate_and_normalize keeps direction but L2-normalizes the vector,
    # so we can't assert raw values — just that the dim is right and that
    # uniform input produces uniform output (all components equal).
    assert len(first["dense_vector"]) == 512
    assert all(abs(c - first["dense_vector"][0]) < 1e-6 for c in first["dense_vector"])
    assert upsert_calls[1]["entry_id"] == "e2"
    assert upsert_calls[1]["model_id"] == "new-embedder"


@pytest.mark.asyncio
async def test_skips_entries_with_empty_embedding_text():
    """An entry whose title + canonical_text are both empty produces no
    embedding text — count it but don't waste a provider call."""
    col = _FakeCollection({"eng": [
        [_make_entry("e1", title="", text="", tags=[])],
        [],
    ]})
    embed = AsyncMock()
    upsert = AsyncMock()
    stats = await reindex_mod.reindex_partition(
        col, "eng",
        new_embedder=None, new_provider=None,
        batch_size=10, dry_run=False, now_ms=1,
        embed_fn=embed, upsert_fn=upsert,
    )
    assert stats["skipped_empty"] == 1
    assert stats["reembedded"] == 0
    assert embed.call_count == 0


@pytest.mark.asyncio
async def test_embed_failure_counted_as_errors_does_not_stop_iteration():
    """A failed embed call increments errors but the next page must still
    be processed — we don't want one transient failure to abort a 50k-entry
    reindex."""
    col = _FakeCollection({"eng": [
        [_make_entry("e1")],
        [_make_entry("e2")],
        [],
    ]})

    async def _embed(texts):
        if any("e1" in t for t in texts) is False and any(
            r.get("entry_id") == "e1" for r in []
        ):
            return [[0.1] * 512]
        # Fail the first batch, succeed on the second.
        if "T\nTopics: alpha\nbody" in texts[0] and not getattr(_embed, "_failed", False):
            _embed._failed = True
            raise RuntimeError("upstream blip")
        return [[0.2] * 512] * len(texts)

    upsert = AsyncMock()
    stats = await reindex_mod.reindex_partition(
        col, "eng",
        new_embedder=None, new_provider=None,
        batch_size=1, dry_run=False, now_ms=1,
        embed_fn=_embed, upsert_fn=upsert,
    )
    assert stats["scanned"] == 2
    assert stats["reembedded"] == 1
    assert stats["errors"] == 1


@pytest.mark.asyncio
async def test_embed_length_mismatch_skips_batch_with_errors():
    """If the provider returns a different number of vectors than texts we
    sent, the batch is unsafe to upsert — bail and count errors."""
    col = _FakeCollection({"eng": [
        [_make_entry("e1"), _make_entry("e2"), _make_entry("e3")],
        [],
    ]})
    # Send 3 inputs, get 2 outputs back — unsafe to zip.
    embed = AsyncMock(return_value=[[0.1] * 512, [0.2] * 512])
    upsert = AsyncMock()
    stats = await reindex_mod.reindex_partition(
        col, "eng",
        new_embedder=None, new_provider=None,
        batch_size=10, dry_run=False, now_ms=1,
        embed_fn=embed, upsert_fn=upsert,
    )
    assert stats["scanned"] == 3
    assert stats["reembedded"] == 0
    assert stats["errors"] == 3
    assert upsert.call_count == 0


# ---------------------------------------------------------------------------
# reindex_all — domain fan-out
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_reindex_all_with_explicit_domain_only_touches_that_partition():
    pages = {
        "eng": [[_make_entry("e1", domain="eng")], []],
        "rag": [[_make_entry("r1", domain="rag")], []],
    }
    col = _FakeCollection(pages)
    out = await reindex_mod.reindex_all(
        new_embedder=None, new_provider=None,
        domain="eng", batch_size=10, dry_run=True,
        collection=col,
    )
    assert set(out.keys()) == {"eng"}
    assert out["eng"]["scanned"] == 1


@pytest.mark.asyncio
async def test_reindex_all_default_fans_across_every_valid_domain():
    """domain=None must cover every partition listed in VALID_DOMAINS so a
    fresh embedder rolls out coherently across the whole corpus."""
    from app.config import VALID_DOMAINS
    pages = {d: [[_make_entry(f"x-{d}", domain=d)], []] for d in VALID_DOMAINS}
    col = _FakeCollection(pages)
    out = await reindex_mod.reindex_all(
        new_embedder=None, new_provider=None,
        domain=None, batch_size=10, dry_run=True,
        collection=col,
    )
    assert set(out.keys()) == VALID_DOMAINS


# ---------------------------------------------------------------------------
# _build_upsert_row — defaults model_id to settings.model_embedder_id when
# new_embedder isn't supplied.
# ---------------------------------------------------------------------------
def test_build_upsert_row_defaults_model_id_to_current_setting():
    from app.config import settings
    src = _make_entry("e1")
    row = reindex_mod._build_upsert_row(src, [0.1] * 4, None, now_ms=42)
    assert row["model_id"] == settings.model_embedder_id
    assert row["updated_at"] == 42
    assert row["entry_id"] == "e1"


def test_build_upsert_row_explicit_new_embedder_wins():
    src = _make_entry("e1")
    row = reindex_mod._build_upsert_row(src, [0.1] * 4, "new-tag", now_ms=42)
    assert row["model_id"] == "new-tag"


# ---------------------------------------------------------------------------
# §17.155 follow-up #2 — cache_metadata.active_embedder_id is written after
# a successful reindex so the next lifespan boot doesn't fire a spurious
# cache.embedder_drift CRITICAL alert.
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_record_active_embedder_upserts_cache_metadata():
    """Mirrors the embedder_drift.check_embedder_drift upsert. The lifespan
    drift check reads ``cache_metadata.active_embedder_id``; if reindex
    doesn't advance it, every first-boot-post-reindex fires a spurious
    drift alert (stored=old, configured=new) even though we just re-embedded
    the corpus to match the new id."""
    from unittest.mock import patch
    mock_db = MagicMock()
    mock_db.__aenter__ = AsyncMock(return_value=mock_db)
    mock_db.__aexit__ = AsyncMock(return_value=None)
    mock_db.execute = AsyncMock()
    mock_db.commit = AsyncMock()

    with patch("app.database.async_session", return_value=mock_db):
        await reindex_mod._record_active_embedder("nomic-embed-text-v2")

    assert mock_db.execute.await_count == 1
    call = mock_db.execute.await_args
    sql_arg = call[0][0]
    params = call[0][1] if len(call[0]) > 1 else call[1]
    sql_str = str(sql_arg)
    assert "cache_metadata" in sql_str
    assert "ON CONFLICT (key) DO UPDATE" in sql_str
    assert params == {"k": "active_embedder_id", "v": "nomic-embed-text-v2"}
    assert mock_db.commit.await_count == 1
