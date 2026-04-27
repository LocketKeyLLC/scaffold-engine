"""Test that semantic near-duplicates are auto-rejected during ingestion."""
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.mark.asyncio
async def test_near_duplicate_rejected():
    """Entry with cosine ≥ dedup threshold should be skipped, not upserted."""
    collection = MagicMock()

    # Exact hash check — no match, so we proceed to semantic check.
    collection.query.return_value = []

    # Semantic search — return a hit above threshold.
    top_hit = MagicMock()
    top_hit.score = 0.98
    top_hit.id = "milvus-pk-42"
    top_hit.entity.get = lambda field, default="": {
        "entry_id": "scaffold-existing-entry-abc12345",
        "content_hash": "different_hash",
        "version": 1,
        "supersedes_id": "",
    }.get(field, default)
    search_result_group = MagicMock()
    search_result_group.__getitem__ = lambda self, idx: top_hit
    search_result_group.__len__ = lambda self: 1
    search_result_group.__bool__ = lambda self: True
    collection.search.return_value = [search_result_group]

    # Mock DB session for dedup_log write.
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

    async def fake_batch(texts):
        return [[0.1] * 512 for _ in texts]

    with patch("app.modules.rag_pipeline._get_collection", return_value=collection), \
         patch("app.modules.rag_pipeline._embed_contents_batch",
               new_callable=AsyncMock, side_effect=fake_batch), \
         patch("app.modules.rag_pipeline.async_session", return_value=mock_session):
        from app.modules.rag_pipeline import ingest_entries
        result = await ingest_entries([test_entry], domain="eng")

    assert result["new"] + result["versioned"] == 0, f"Expected 0 inserted, got {result}"
    assert result["rejected"] == 1, f"Expected 1 rejection, got {result}"

    # Neither insert nor upsert should have been called on a rejected entry.
    collection.insert.assert_not_called()
    collection.upsert.assert_not_called()

    # dedup_log should have been written.
    mock_session.execute.assert_called_once()
    mock_session.commit.assert_called_once()
