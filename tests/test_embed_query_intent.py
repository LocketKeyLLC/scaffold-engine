"""Tests for §17.118 — per-intent embedder instruction templates.

embed_query(query, query_intent=...) selects the prefix from
EMBED_QUERY_TEMPLATES. Different intents → different cache keys →
different embeddings (so retrieval can lean into the intent's
embedding-space neighborhood).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture
def mock_cache():
    """Replace the embedding cache with a miss-only fake; record keys."""
    cache = MagicMock()
    cache.get = AsyncMock(return_value=None)
    cache.put = AsyncMock(return_value=True)
    with patch("app.utils.embedding.get_cache", return_value=cache):
        yield cache


@pytest.fixture
def mock_embedder():
    """Replace model_router.embed with a recorder."""
    embed = AsyncMock(return_value=[[0.1] * 512])
    with patch("app.utils.embedding.model_router.embed", embed), \
         patch("app.utils.embedding.truncate_and_normalize",
               side_effect=lambda v: v[:512]):
        yield embed


@pytest.mark.smoke
class TestEmbedQueryTemplates:
    def test_all_known_intents_distinct(self):
        from app.utils.embedding import EMBED_QUERY_TEMPLATES
        seen = set(EMBED_QUERY_TEMPLATES.values())
        assert len(seen) == len(EMBED_QUERY_TEMPLATES), \
            "templates must be distinct so cache keys diverge across intents"

    def test_general_is_default(self):
        from app.utils.embedding import EMBED_QUERY_TEMPLATES
        assert "general" in EMBED_QUERY_TEMPLATES
        assert "Given a query, retrieve relevant knowledge" in EMBED_QUERY_TEMPLATES["general"]

    def test_specialized_intents_present(self):
        from app.utils.embedding import EMBED_QUERY_TEMPLATES
        for intent in ("code", "qa", "paper"):
            assert intent in EMBED_QUERY_TEMPLATES


@pytest.mark.asyncio
async def test_embed_query_uses_general_template_by_default(mock_cache, mock_embedder):
    from app.utils.embedding import embed_query, EMBED_QUERY_TEMPLATES
    await embed_query("what is foo")
    sent = mock_embedder.call_args[0][0]
    assert sent.startswith(EMBED_QUERY_TEMPLATES["general"])
    assert sent.endswith("what is foo")


@pytest.mark.asyncio
async def test_embed_query_uses_code_template_when_requested(mock_cache, mock_embedder):
    from app.utils.embedding import embed_query, EMBED_QUERY_TEMPLATES
    await embed_query("how to call foo", query_intent="code")
    sent = mock_embedder.call_args[0][0]
    assert sent.startswith(EMBED_QUERY_TEMPLATES["code"])


@pytest.mark.asyncio
async def test_embed_query_cache_keys_diverge_by_intent(mock_cache, mock_embedder):
    from app.utils.embedding import embed_query
    await embed_query("x", query_intent="general")
    await embed_query("x", query_intent="code")
    # Two distinct cache lookups; same query, different intents.
    assert mock_cache.get.call_count == 2
    seen_keys = [c.args[0] for c in mock_cache.get.call_args_list]
    assert seen_keys[0] != seen_keys[1], \
        "cache keys must diverge across intents"


@pytest.mark.asyncio
async def test_embed_query_unknown_intent_falls_back_to_general(
    mock_cache, mock_embedder, caplog,
):
    from app.utils.embedding import embed_query, EMBED_QUERY_TEMPLATES
    with caplog.at_level("DEBUG", logger="app.utils.embedding"):
        await embed_query("x", query_intent="not-a-real-intent")
    sent = mock_embedder.call_args[0][0]
    assert sent.startswith(EMBED_QUERY_TEMPLATES["general"])
    assert any("embed_query_unknown_intent" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_embed_query_cache_hit_short_circuits(mock_embedder):
    """Cache hit returns immediately, embedder NOT called."""
    from app.utils.embedding import embed_query
    cache = MagicMock()
    cache.get = AsyncMock(return_value=[0.5] * 512)
    cache.put = AsyncMock(return_value=True)
    with patch("app.utils.embedding.get_cache", return_value=cache):
        result = await embed_query("x", query_intent="code")
    assert result == [0.5] * 512
    mock_embedder.assert_not_called()


# ---------------------------------------------------------------------------
# RagInput schema validation
# ---------------------------------------------------------------------------

@pytest.mark.smoke
class TestRagInputQueryIntent:
    def test_default_is_general(self):
        from app.schemas import RagInput
        body = RagInput(query="x")
        assert body.query_intent == "general"

    def test_accepts_known_intents(self):
        from app.schemas import RagInput
        for intent in ("general", "code", "qa", "paper"):
            body = RagInput(query="x", query_intent=intent)
            assert body.query_intent == intent

    def test_rejects_unknown_intent(self):
        from app.schemas import RagInput
        from pydantic import ValidationError
        with pytest.raises(ValidationError):
            RagInput(query="x", query_intent="bogus")
