"""§17.431 — unit tests for the BM25 hybrid additions.

Offline: schema/function presence, the runtime BM25-field detector, and the
_keyword_search dispatcher routing (BM25 vs LIKE). The live end-to-end path
(real Milvus BM25 search + the migration script) was validated on a throwaway
collection during implementation.
"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from app import rerankers  # noqa: F401 — ensures app import graph is healthy
from app.config import settings
from app.modules import rag_pipeline
from app.utils import milvus_utils
from app.utils.milvus_utils import (
    BM25_SPARSE_FIELD,
    build_toon_v2_schema,
    collection_has_bm25,
)

pytestmark = pytest.mark.smoke


# --- schema builder ---

def test_schema_bm25_off_is_unchanged_16_fields():
    s = build_toon_v2_schema(bm25=False)
    assert len(s.fields) == 16
    assert BM25_SPARSE_FIELD not in {f.name for f in s.fields}
    assert len(getattr(s, "functions", []) or []) == 0


def test_schema_bm25_on_adds_sparse_field_and_function():
    s = build_toon_v2_schema(bm25=True)
    names = {f.name for f in s.fields}
    assert BM25_SPARSE_FIELD in names
    assert len(s.fields) == 17
    assert len(getattr(s, "functions", []) or []) == 1


def test_schema_default_follows_settings(monkeypatch):
    monkeypatch.setattr(settings, "rag_bm25_enabled", False)
    assert len(build_toon_v2_schema().fields) == 16
    monkeypatch.setattr(settings, "rag_bm25_enabled", True)
    assert BM25_SPARSE_FIELD in {f.name for f in build_toon_v2_schema().fields}


# --- collection_has_bm25 detector ---

def _fake_collection(field_names):
    return SimpleNamespace(
        schema=SimpleNamespace(fields=[SimpleNamespace(name=n) for n in field_names])
    )


def test_collection_has_bm25_true():
    col = _fake_collection(["entry_id", "canonical_text", BM25_SPARSE_FIELD])
    assert collection_has_bm25(col) is True


def test_collection_has_bm25_false():
    col = _fake_collection(["entry_id", "canonical_text", "dense_vector"])
    assert collection_has_bm25(col) is False


def test_collection_has_bm25_handles_broken_schema():
    assert collection_has_bm25(object()) is False  # no .schema → False, not raise


# --- _keyword_search dispatcher ---

@pytest.fixture
def routed(monkeypatch):
    """Patch both backends to async sentinels; return them for assertions."""
    bm25 = AsyncMock(return_value=["BM25"])
    like = AsyncMock(return_value=["LIKE"])
    monkeypatch.setattr(rag_pipeline, "_bm25_search", bm25)
    monkeypatch.setattr(rag_pipeline, "_keyword_search_like", like)
    return bm25, like


@pytest.mark.asyncio
async def test_dispatch_to_bm25_when_enabled_and_migrated(routed, monkeypatch):
    bm25, like = routed
    monkeypatch.setattr(settings, "rag_bm25_enabled", True)
    monkeypatch.setattr(milvus_utils, "collection_has_bm25", lambda c: True)
    out = await rag_pipeline._keyword_search(object(), "q", 5, "eng")
    assert out == ["BM25"]
    bm25.assert_awaited_once()
    like.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_to_like_when_flag_off(routed, monkeypatch):
    bm25, like = routed
    monkeypatch.setattr(settings, "rag_bm25_enabled", False)
    monkeypatch.setattr(milvus_utils, "collection_has_bm25", lambda c: True)
    out = await rag_pipeline._keyword_search(object(), "q", 5, "eng")
    assert out == ["LIKE"]
    like.assert_awaited_once()
    bm25.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_to_like_when_not_migrated(routed, monkeypatch):
    bm25, like = routed
    monkeypatch.setattr(settings, "rag_bm25_enabled", True)
    monkeypatch.setattr(milvus_utils, "collection_has_bm25", lambda c: False)
    out = await rag_pipeline._keyword_search(object(), "q", 5, "eng")
    assert out == ["LIKE"]
    like.assert_awaited_once()
    bm25.assert_not_awaited()


@pytest.mark.asyncio
async def test_dispatch_to_like_when_collection_none(routed, monkeypatch):
    bm25, like = routed
    monkeypatch.setattr(settings, "rag_bm25_enabled", True)
    out = await rag_pipeline._keyword_search(None, "q", 5, "eng")
    assert out == ["LIKE"]
    like.assert_awaited_once()
    bm25.assert_not_awaited()
