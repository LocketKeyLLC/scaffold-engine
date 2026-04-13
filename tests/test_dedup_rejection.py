"""Test that semantic near-duplicates are auto-rejected during ingestion."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.mark.asyncio
async def test_near_duplicate_rejected():
    """Entry with cosine > threshold should be skipped, not inserted."""

    # Mock Milvus collection
    collection = MagicMock()

    # Exact hash check — no match (so we proceed to semantic check)
    collection.query.return_value = []

    # Semantic search — return a hit above threshold
    top_hit = MagicMock()
    top_hit.score = 0.98
    top_hit.id = "milvus-pk-42"
    top_hit.entity.get = lambda field, default="": {
        "entry_id": "scaffold-existing-entry-abc12345",
        "content_hash": "different_hash",
    }.get(field, default)

    search_result_group = MagicMock()
    search_result_group.__getitem__ = lambda self, idx: top_hit
    search_result_group.__len__ = lambda self: 1
    search_result_group.__bool__ = lambda self: True
    collection.search.return_value = [search_result_group]

    # Mock DB session for dedup_log write
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    test_entry = {
        "title": "Near Duplicate Test Entry",
        "content": "This content is very similar to something already ingested.",
        "domain_tags": ["testing"],
        "source_type": "tech_docs",
        "confidence_score": 0.85,
    }

    with patch("app.modules.rag_pipeline._get_collection", return_value=collection), \
         patch("app.modules.rag_pipeline._embed_content", new_callable=AsyncMock, return_value=[0.1] * 512), \
         patch("app.modules.rag_pipeline.async_session", return_value=mock_session), \
         patch("app.modules.rag_pipeline.settings") as mock_settings:

        mock_settings.semantic_dedup_threshold = 0.95
        mock_settings.model_embedder_id = "qwen3-embedding:8b"

        from app.modules.rag_pipeline import ingest_entries
        result = await ingest_entries([test_entry], domain="eng")

    # Should have inserted 0 entries
    assert result == 0, f"Expected 0 ingested, got {result}"

    # insert() should never have been called
    collection.insert.assert_not_called()

    # dedup_log should have been written
    mock_session.execute.assert_called_once()
    mock_session.commit.assert_called_once()
