"""Tests for app/utils/embedding.py (#6.19)."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.utils import embedding


@pytest.mark.smoke
def test_embed_query_is_public():
    from app.utils.embedding import embed_query
    assert callable(embed_query)


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_embed_query_returns_512d_vector():
    raw = [0.1] * 4096
    truncated = [0.1] * 512
    fake_cache = MagicMock()
    fake_cache.get = AsyncMock(return_value=None)
    fake_cache.put = AsyncMock(return_value=None)

    with patch.object(embedding, "get_cache", return_value=fake_cache), \
         patch.object(embedding.model_router, "embed", AsyncMock(return_value=[raw])), \
         patch.object(embedding, "truncate_and_normalize", return_value=truncated):
        vec = await embedding.embed_query("sample query")

    assert vec is not None
    assert len(vec) == 512


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_embed_query_returns_none_on_empty():
    fake_cache = MagicMock()
    fake_cache.get = AsyncMock(return_value=None)
    fake_cache.put = AsyncMock(return_value=None)
    with patch.object(embedding, "get_cache", return_value=fake_cache), \
         patch.object(embedding.model_router, "embed", AsyncMock(return_value=[])):
        vec = await embedding.embed_query("x")
    assert vec is None


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_embed_query_hits_cache():
    cached = [0.2] * 512
    fake_cache = MagicMock()
    fake_cache.get = AsyncMock(return_value=cached)
    fake_cache.put = AsyncMock(return_value=None)
    with patch.object(embedding, "get_cache", return_value=fake_cache):
        vec = await embedding.embed_query("cached query")
    assert vec == cached
