"""Test that semantic near-duplicates are auto-rejected during ingestion.

§17.172 adds tests for the dedup_log↔Milvus upsert atomicity contract: a
'versioned' dedup_log row must only exist if the corresponding Milvus
version chain successfully upserted. Pre-§17.172 the INSERT ran before
the upsert, so an upsert failure left the audit ledger with a row for
a chain that didn't materialize.
"""
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


# ── §17.172 — dedup_log ↔ Milvus upsert atomicity ─────────────────────


def _make_collection_with_supersede_match(sim_score: float = 0.92):
    """Build a Milvus collection mock whose semantic search returns a
    single hit in the version-chain band (>= version_threshold,
    < dedup_threshold). Default sim_score 0.92 falls inside the default
    band of [0.90, 0.95). The exact-hash query returns empty so the
    semantic path is exercised."""
    collection = MagicMock()
    collection.query.return_value = []  # no exact-hash hit → semantic path
    top_hit = MagicMock()
    top_hit.score = sim_score
    top_hit.id = "milvus-pk-100"
    top_hit.entity.get = lambda field, default="": {
        "entry_id": "scaffold-existing-old-version-abc12345",
        "content_hash": "different_hash",
        "version": 1,
        "supersedes_id": "",
    }.get(field, default)
    search_result_group = MagicMock()
    search_result_group.__getitem__ = lambda self, idx: top_hit
    search_result_group.__len__ = lambda self: 1
    search_result_group.__bool__ = lambda self: True
    collection.search.return_value = [search_result_group]
    return collection


@pytest.mark.asyncio
async def test_version_chain_writes_dedup_log_after_upsert_succeeds():
    """Happy path: version-chain decision + successful Milvus upsert →
    exactly one dedup_log row with action='versioned'. Verifies the
    post-§17.172 ordering (append AFTER upsert) doesn't drop the
    audit row on the success path."""
    collection = _make_collection_with_supersede_match(sim_score=0.92)
    # _walk_to_latest_version uses collection.query to walk forward.
    # Our top_hit has no successor (supersedes_id == "") so the walker
    # returns the candidate unchanged → that becomes the supersedes_id.

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    test_entry = {
        "title": "New Version Entry",
        "content": "Updated content that is similar but not identical to the old version.",
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

    assert result["versioned"] == 1
    assert result["rejected"] == 0
    # Upsert MUST have been called.
    collection.upsert.assert_called_once()
    # And the dedup_log INSERT MUST have been issued (in the batched
    # commit at the end of ingest_entries).
    sql_calls = [
        call for call in mock_session.execute.await_args_list
        if "dedup_log" in str(call.args[0])
    ]
    assert len(sql_calls) == 1, f"Expected 1 dedup_log INSERT, got {len(sql_calls)}"
    # The action must be 'versioned' (not 'rejected').
    bind_params = sql_calls[0].args[1]
    assert bind_params["action"] == "versioned"


@pytest.mark.asyncio
async def test_version_chain_skips_dedup_log_when_upsert_fails():
    """Invariant: if the Milvus upsert raises, NO 'versioned' row may
    land in dedup_log. Pre-§17.172 the INSERT preceded the upsert and
    survived the failure, leaving the audit ledger pointing at a
    chain that never existed."""
    collection = _make_collection_with_supersede_match(sim_score=0.92)
    collection.upsert.side_effect = RuntimeError("milvus unavailable")

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    test_entry = {
        "title": "Should-Be-Versioned Entry",
        "content": "Content similar enough to trigger version chain.",
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

    # Upsert was attempted but failed; stats reflect "neither new nor
    # versioned" because the success-branch increments are gated on the
    # upsert returning cleanly.
    assert result["versioned"] == 0
    assert result["new"] == 0
    collection.upsert.assert_called_once()
    # CRITICAL invariant: no dedup_log row written. The batched commit
    # block at end of ingest_entries should observe an empty list and
    # skip the async_session() entirely.
    sql_calls = [
        call for call in mock_session.execute.await_args_list
        if "dedup_log" in str(call.args[0])
    ]
    assert len(sql_calls) == 0, (
        f"Expected NO dedup_log INSERT after failed upsert, got {len(sql_calls)}: "
        f"this is the §17.172 atomicity bug."
    )


@pytest.mark.asyncio
async def test_rejected_dedup_log_uses_batched_commit():
    """§17.172 — the rejection path also flows through the batched
    dedup_log_writes accumulator (rather than its own per-entry
    session). Net behavior unchanged: a single INSERT, a single
    commit. Guards against regressing back to per-entry sessions
    if someone re-touches this code."""
    # Same fixture as test_near_duplicate_rejected but with an explicit
    # check that the batched commit pattern is in effect.
    collection = MagicMock()
    collection.query.return_value = []
    top_hit = MagicMock()
    top_hit.score = 0.98  # above dedup threshold → rejection branch
    top_hit.id = "milvus-pk-1"
    top_hit.entity.get = lambda field, default="": {
        "entry_id": "scaffold-existing-abc12345",
        "content_hash": "h",
        "version": 1,
        "supersedes_id": "",
    }.get(field, default)
    grp = MagicMock()
    grp.__getitem__ = lambda self, idx: top_hit
    grp.__len__ = lambda self: 1
    grp.__bool__ = lambda self: True
    collection.search.return_value = [grp]

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async def fake_batch(texts):
        return [[0.1] * 512 for _ in texts]

    with patch("app.modules.rag_pipeline._get_collection", return_value=collection), \
         patch("app.modules.rag_pipeline._embed_contents_batch",
               new_callable=AsyncMock, side_effect=fake_batch), \
         patch("app.modules.rag_pipeline.async_session", return_value=mock_session):
        from app.modules.rag_pipeline import ingest_entries
        result = await ingest_entries(
            [{"title": "T", "content": "C" * 50, "domain_tags": [],
              "source_type": "tech_docs", "confidence_score": 0.85}],
            domain="eng",
        )

    assert result["rejected"] == 1
    # One dedup_log INSERT, action='rejected'.
    sql_calls = [
        call for call in mock_session.execute.await_args_list
        if "dedup_log" in str(call.args[0])
    ]
    assert len(sql_calls) == 1
    assert sql_calls[0].args[1]["action"] == "rejected"
