"""Test that semantic near-duplicates are auto-rejected during ingestion.

§17.172 adds tests for the dedup_log↔Milvus upsert atomicity contract: a
'versioned' dedup_log row must only exist if the corresponding Milvus
version chain successfully upserted. Pre-§17.172 the INSERT ran before
the upsert, so an upsert failure left the audit ledger with a row for
a chain that didn't materialize.

§17.267 adds the version-chain concurrency-race documentation tests —
two ingests that BOTH match the same existing entry in the version-
chain band (cosine 0.90-0.95), interleaved such that neither sees the
other's upsert, both compute supersedes_id == matched_id and both
upsert rows pointing at the same predecessor. The chain branches.
"""
import asyncio
import pytest
from unittest.mock import patch, MagicMock, AsyncMock


@pytest.mark.asyncio
async def test_near_duplicate_rejected():
    """Entry with cosine ≥ dedup threshold should be skipped, not upserted."""
    collection = MagicMock()

    # Exact hash check — no match, so we proceed to semantic check.
    collection.query.return_value = []

    # Semantic search — return a hit above threshold (MilvusClient shape).
    collection.search.return_value = [[
        {"distance": 0.98, "id": "milvus-pk-42", "entity": {
            "entry_id": "scaffold-existing-entry-abc12345",
            "content_hash": "different_hash",
            "version": 1,
            "supersedes_id": "",
        }},
    ]]

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

    with patch("app.modules.rag_pipeline._get_client", return_value=collection), \
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
    collection.search.return_value = [[
        {"distance": sim_score, "id": "milvus-pk-100", "entity": {
            "entry_id": "scaffold-existing-old-version-abc12345",
            "content_hash": "different_hash",
            "version": 1,
            "supersedes_id": "",
        }},
    ]]
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

    with patch("app.modules.rag_pipeline._get_client", return_value=collection), \
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

    with patch("app.modules.rag_pipeline._get_client", return_value=collection), \
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
    # above dedup threshold → rejection branch (MilvusClient shape)
    collection.search.return_value = [[
        {"distance": 0.98, "id": "milvus-pk-1", "entity": {
            "entry_id": "scaffold-existing-abc12345",
            "content_hash": "h",
            "version": 1,
            "supersedes_id": "",
        }},
    ]]

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async def fake_batch(texts):
        return [[0.1] * 512 for _ in texts]

    with patch("app.modules.rag_pipeline._get_client", return_value=collection), \
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


# ──────────────────────────────────────────────────────────────────────
# §17.267 — version-chain concurrency race (test-gap from §17.258)
# ──────────────────────────────────────────────────────────────────────
#
# The race window: two ingests T1 and T2 both call _walk_to_latest_version
# on the same matched_id A while neither's upsert has landed in Milvus.
# Both walks return (A, 1) because no successor is visible yet. Both
# compute new_supersedes = A, new_version = 2. Both upsert. Result:
# two distinct rows at version=2, both with supersedes_id=A → the chain
# branches. The "version chain is a singly-linked list" invariant is
# broken.
#
# §17.269 LANDS THE FIX — _predecessor_lock wraps walk+upsert in a
# Postgres advisory lock keyed on the predecessor entry_id. Two ingests
# with the same predecessor serialize; T2's re-walk inside the lock sees
# T1's commit; T2 links to T1's row → linear chain. The fix's
# end-to-end demonstration lives in test_sequential_ingest_produces_
# linear_chain below; the test_concurrent_ingest_branches_version_chain
# test continues to pass as documentation of the race window itself
# (mocked Postgres cannot enforce real serialization, so the in-mock
# branch is the "what would happen if the lock failed to acquire" case).


@pytest.mark.asyncio
async def test_walk_returns_same_eid_when_no_successor_exists():
    """§17.267 race precondition: when no row points at A via
    supersedes_id=A, _walk_to_latest_version(A, 1) returns (A, 1).
    Both ingests in the race window observe exactly this state — there
    is no synchronization on the walk, so two concurrent calls both
    compute new_supersedes=A and produce a branch on upsert."""
    from app.modules.rag_pipeline import _walk_to_latest_version

    collection = MagicMock()
    collection.query.return_value = []  # no successor — race-window state

    # Concurrent walks targeting the same eid — the realistic race shape.
    results = await asyncio.gather(
        _walk_to_latest_version(collection, "A", 1, "eng"),
        _walk_to_latest_version(collection, "A", 1, "eng"),
    )

    # Both must return the same (eid, version) — that's the race
    # precondition. After both upsert, the chain has TWO version=2 rows
    # both pointing at A.
    assert results[0] == ("A", 1), f"expected (A, 1), got {results[0]}"
    assert results[1] == ("A", 1), f"expected (A, 1), got {results[1]}"

    # The walk made queries — confirms it didn't short-circuit on an
    # in-process cache (which would mask the race differently).
    assert collection.query.call_count >= 2, (
        f"expected at least 2 query calls (one per walk); "
        f"got {collection.query.call_count}"
    )


@pytest.mark.asyncio
async def test_concurrent_ingest_branches_version_chain():
    """§17.267 end-to-end race demonstration. Two ingests run via
    asyncio.gather; both match the same existing entry A at sim=0.92
    (version-chain band). Both walk forward, find no successor, set
    supersedes_id=A. Both upsert. Expected post-§17.267 state: TWO
    upserted rows, BOTH with supersedes_id == the same value.

    This is the bug. The test ASSERTS the broken behavior so the
    invariant violation is pinned to a specific failure mode. When a
    fix lands (likely a §17.x advisory-lock or CAS-upsert), this test
    will need to flip: assert exactly one upsert succeeds and the other
    is rejected (or retries onto the new tail). Until then, the test
    is the regression guard against accidental walk-semantics changes
    that would obscure the race without addressing it."""
    collection = _make_collection_with_supersede_match(sim_score=0.92)

    # Two entries with DIFFERENT content (so they have different hashes
    # and aren't caught by the Pass 1 exact-hash filter), both close
    # enough to A to land in the version-chain band.
    entry_1 = {
        "title": "Race Entry One",
        "content": "First concurrent version targeting the same predecessor.",
        "domain_tags": ["testing"],
        "source_type": "tech_docs",
        "confidence_score": 0.85,
    }
    entry_2 = {
        "title": "Race Entry Two",
        "content": "Second concurrent version, different bytes, same target.",
        "domain_tags": ["testing"],
        "source_type": "tech_docs",
        "confidence_score": 0.85,
    }

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async def fake_batch(texts):
        return [[0.1] * 512 for _ in texts]

    with patch("app.modules.rag_pipeline._get_client", return_value=collection), \
         patch("app.modules.rag_pipeline._embed_contents_batch",
               new_callable=AsyncMock, side_effect=fake_batch), \
         patch("app.modules.rag_pipeline.async_session", return_value=mock_session):
        from app.modules.rag_pipeline import ingest_entries
        # Race them via asyncio.gather. Neither sees the other's upsert
        # because the mock doesn't update collection.search results on
        # upsert — exactly the production race window where the writes
        # haven't propagated yet.
        results = await asyncio.gather(
            ingest_entries([entry_1], domain="eng"),
            ingest_entries([entry_2], domain="eng"),
        )

    # Both ingests should report 1 versioned (the bug: both branches
    # accept the version-chain decision independently).
    assert results[0]["versioned"] == 1, f"got {results[0]}"
    assert results[1]["versioned"] == 1, f"got {results[1]}"

    # Both upserts fired — extract supersedes_id from each.
    upsert_calls = collection.upsert.call_args_list
    assert len(upsert_calls) == 2, (
        f"expected 2 upsert calls (one per concurrent ingest); "
        f"got {len(upsert_calls)}"
    )
    supersedes_ids = []
    for c in upsert_calls:
        # §17.591 — upsert called as upsert(collection_name=…, data=[{...}])
        row = c.kwargs["data"][0]
        supersedes_ids.append(row["supersedes_id"])

    # LOAD-BEARING ASSERTION (documents the bug):
    # Both upserts point at the SAME supersedes_id. Production result:
    # two rows at version=2, both with supersedes_id=A. The chain is
    # branched. When the race is fixed, this assertion must flip — see
    # the test docstring for the fix-arrival contract.
    assert supersedes_ids[0] == supersedes_ids[1], (
        f"§17.267 race documentation: expected both upserts to point at "
        f"the same predecessor (the bug); got {supersedes_ids}"
    )
    # And both at version=2 (each independently concluded latest+1).
    versions = [c.kwargs["data"][0]["version"] for c in upsert_calls]
    assert versions == [2, 2], (
        f"expected both at version=2 (the branch); got {versions}"
    )


# ──────────────────────────────────────────────────────────────────────
# §17.269 — version-chain race FIX via Postgres advisory lock
# ──────────────────────────────────────────────────────────────────────


def test_advisory_lock_key_is_stable_and_distinct():
    """§17.269 — _advisory_lock_key("A") returns the same value across
    processes (so two ingests cluster on the lock) and different values
    for different predecessors (so unrelated ingests don't contend)."""
    from app.modules.rag_pipeline import _advisory_lock_key

    k_a = _advisory_lock_key("scaffold-entry-A")
    k_a_again = _advisory_lock_key("scaffold-entry-A")
    k_b = _advisory_lock_key("scaffold-entry-B")

    # Stability — same input → same key (two ingests cluster on the lock).
    assert k_a == k_a_again, f"hash should be deterministic; got {k_a} vs {k_a_again}"
    # Distinctness — different predecessors → different keys (no false contention).
    assert k_a != k_b, f"hash should distinguish predecessors; got {k_a} == {k_b}"
    # Range check — must fit in Postgres bigint (signed 64-bit).
    assert -(2**63) <= k_a < 2**63, f"key {k_a} out of bigint range"


@pytest.mark.asyncio
async def test_predecessor_lock_issues_pg_advisory_xact_lock():
    """§17.269 — _predecessor_lock context manager issues
    pg_advisory_xact_lock with the SHA256-derived key on enter and
    commits on exit (releasing the lock). Verifies the SQL contract;
    real Postgres serialization is operator-side."""
    from app.modules.rag_pipeline import _predecessor_lock, _advisory_lock_key

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("app.modules.rag_pipeline.async_session", return_value=mock_session):
        async with _predecessor_lock("scaffold-entry-X") as db:
            assert db is mock_session, "context manager must yield the live session"

    # Lock acquisition SQL must have fired with the expected key.
    expected_key = _advisory_lock_key("scaffold-entry-X")
    lock_calls = [
        c for c in mock_session.execute.await_args_list
        if "pg_advisory_xact_lock" in str(c.args[0])
    ]
    assert len(lock_calls) == 1, (
        f"expected exactly 1 pg_advisory_xact_lock call; got {len(lock_calls)}"
    )
    assert lock_calls[0].args[1] == {"k": expected_key}, (
        f"lock key mismatch; expected {{'k': {expected_key}}}, "
        f"got {lock_calls[0].args[1]}"
    )
    # Commit must have been called (releases the lock).
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_predecessor_lock_rolls_back_on_inner_exception():
    """§17.269 — if the body raises, the lock is released via rollback
    (still ends the transaction, still releases pg_advisory_xact_lock)
    rather than leaving the lock dangling."""
    from app.modules.rag_pipeline import _predecessor_lock

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    with patch("app.modules.rag_pipeline.async_session", return_value=mock_session):
        with pytest.raises(RuntimeError, match="boom"):
            async with _predecessor_lock("scaffold-entry-Y"):
                raise RuntimeError("boom")

    mock_session.rollback.assert_awaited_once()
    mock_session.commit.assert_not_awaited()


def _make_stateful_collection_with_supersede_match(sim_score: float = 0.92):
    """Like _make_collection_with_supersede_match but `collection.upsert`
    captures rows into a list and `collection.query` walks that list for
    `supersedes_id == X` lookups. Lets _walk_to_latest_version see prior
    upserts within the same test, which is what production Milvus does
    after a flush. Exposes the captured rows as `collection._upserted_rows`."""
    import re as _re
    collection = MagicMock()
    upserted_rows: list[dict] = []

    # Semantic search — always returns existing entry A as top hit
    # (MilvusClient shape).
    collection.search.return_value = [[
        {"distance": sim_score, "id": "milvus-pk-100", "entity": {
            "entry_id": "scaffold-entry-A",
            "content_hash": "different_hash",
            "version": 1,
            "supersedes_id": "",
        }},
    ]]

    def stateful_query(*, collection_name=None, filter="", output_fields=None,
                       limit=None, **kwargs):
        # §17.591 — MilvusClient signature (filter= replaces expr=).
        # Exact-hash query (Pass 1) — always [] so we exercise the semantic path.
        if "content_hash" in filter:
            return []
        # Walk query — match upserted rows whose supersedes_id equals the target.
        m = _re.search(r'supersedes_id == "([^"]+)"', filter)
        if not m:
            return []
        target = m.group(1)
        hits = [
            {"entry_id": r["entry_id"], "version": r["version"]}
            for r in upserted_rows
            if r.get("supersedes_id") == target
        ]
        return hits[:limit] if limit else hits

    collection.query.side_effect = stateful_query

    def stateful_upsert(*, collection_name=None, data=None, **kwargs):
        # §17.591 — MilvusClient signature (data= replaces positional rows).
        upserted_rows.extend(data or [])

    collection.upsert.side_effect = stateful_upsert
    collection._upserted_rows = upserted_rows  # for assertions
    return collection


@pytest.mark.asyncio
async def test_sequential_ingest_produces_linear_chain_post_fix():
    """§17.269 — END-TO-END fix verification (sequential case).

    With the advisory lock + re-walk-inside-lock, two ingests that
    both match the same predecessor at sim=0.92 produce a LINEAR
    chain: T1's row points at A; T2's row points at T1's row (not A).

    Uses a stateful collection mock so the second ingest's walk sees
    the first ingest's row — what production Milvus does after the
    upsert is visible. The advisory-lock SERIALIZATION is verified
    independently in test_predecessor_lock_issues_pg_advisory_xact_lock;
    this test verifies the chain-linking logic under that serialization.

    Concurrent end-to-end verification with REAL serialization needs
    live Postgres + Milvus and is logged as a separate integration test."""
    collection = _make_stateful_collection_with_supersede_match(sim_score=0.92)

    entry_1 = {
        "title": "Linear Chain Entry One",
        "content": "First version targeting A in the version-chain band.",
        "domain_tags": ["testing"],
        "source_type": "tech_docs",
        "confidence_score": 0.85,
    }
    entry_2 = {
        "title": "Linear Chain Entry Two",
        "content": "Second version, different content, same target predecessor.",
        "domain_tags": ["testing"],
        "source_type": "tech_docs",
        "confidence_score": 0.85,
    }

    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    async def fake_batch(texts):
        return [[0.1] * 512 for _ in texts]

    with patch("app.modules.rag_pipeline._get_client", return_value=collection), \
         patch("app.modules.rag_pipeline._embed_contents_batch",
               new_callable=AsyncMock, side_effect=fake_batch), \
         patch("app.modules.rag_pipeline.async_session", return_value=mock_session):
        from app.modules.rag_pipeline import ingest_entries
        # Sequential — T1 fully completes (committed + lock released)
        # before T2 starts. This is the serialized order the advisory
        # lock guarantees in production.
        r1 = await ingest_entries([entry_1], domain="eng")
        r2 = await ingest_entries([entry_2], domain="eng")

    assert r1["versioned"] == 1, f"T1: {r1}"
    assert r2["versioned"] == 1, f"T2: {r2}"

    rows = collection._upserted_rows
    assert len(rows) == 2, f"expected 2 upserts; got {len(rows)}"

    # T1 points at the original predecessor A.
    assert rows[0]["supersedes_id"] == "scaffold-entry-A", (
        f"T1 should link to A; got {rows[0]['supersedes_id']}"
    )
    assert rows[0]["version"] == 2

    # LOAD-BEARING: T2 must link to T1's row (R1), NOT to A.
    # That's the linear chain post-§17.269. Pre-fix, T2 would have
    # also linked to A (the branch).
    assert rows[1]["supersedes_id"] == rows[0]["entry_id"], (
        f"§17.269 linear chain: T2.supersedes_id must equal T1.entry_id "
        f"({rows[0]['entry_id']}); got {rows[1]['supersedes_id']}"
    )
    assert rows[1]["version"] == 3, (
        f"T2 should be at version=3 (T1.version + 1 after re-walk); "
        f"got {rows[1]['version']}"
    )
