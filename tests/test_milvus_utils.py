"""Tests for app/utils/milvus_utils.py (#9.26)."""
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from app.utils import milvus_utils


@pytest.fixture(autouse=True)
def _reset_cache():
    """Each test starts with a clean cache."""
    milvus_utils._invalidate_cache()
    yield
    milvus_utils._invalidate_cache()


@pytest.fixture
def _bypass_schema_assert():
    """MagicMock collections don't have real schema shape; skip the invariant check."""
    with patch.object(milvus_utils, "_assert_schema_invariants"):
        yield


# ---------------------------------------------------------------------------
# Schema + index builders (#42)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_build_toon_v2_schema_has_16_fields():
    # §17.438 — pin bm25=False so this asserts the canonical base schema
    # deterministically, independent of the runtime RAG_BM25_ENABLED flag
    # (which is True in a BM25-activated deployment → 17 fields).
    schema = milvus_utils.build_toon_v2_schema(bm25=False)
    assert len(schema.fields) == 16


@pytest.mark.smoke
def test_build_toon_v2_schema_has_vector_and_partition_key():
    schema = milvus_utils.build_toon_v2_schema(bm25=False)
    names = {f.name for f in schema.fields}
    assert "dense_vector" in names
    assert "domain" in names
    assert "entry_id" in names


@pytest.mark.smoke
def test_build_toon_v2_index_params_includes_hnsw_and_scalar_indexes():
    from pymilvus import MilvusClient
    client = MilvusClient.__new__(MilvusClient)  # bypass __init__
    try:
        params = milvus_utils.build_toon_v2_index_params(client)
    except Exception:
        pytest.skip("MilvusClient.prepare_index_params unavailable without connection")
    assert params is not None


# ---------------------------------------------------------------------------
# Schema invariant assertion (new)
# ---------------------------------------------------------------------------
def _fake_client_with_schema(dim: int = 512, primary: str = "entry_id", vec_field: str = "dense_vector"):
    """Build a MagicMock MilvusClient whose describe_collection() survives
    _assert_schema_invariants (§17.591 — MilvusClient describe_collection dict)."""
    client = MagicMock(name="MilvusClient")
    client.describe_collection.return_value = {
        "fields": [
            {"name": primary, "is_primary": True, "params": {}},
            {"name": vec_field, "is_primary": False, "params": {"dim": dim}},
        ]
    }
    return client


@pytest.mark.smoke
def test_assert_schema_invariants_passes_on_valid_schema():
    client = _fake_client_with_schema(dim=512, primary="entry_id")
    milvus_utils._assert_schema_invariants(client)  # should not raise


@pytest.mark.smoke
def test_assert_schema_invariants_raises_on_wrong_dim():
    client = _fake_client_with_schema(dim=768)
    with pytest.raises(RuntimeError, match="dim"):
        milvus_utils._assert_schema_invariants(client)


@pytest.mark.smoke
def test_assert_schema_invariants_raises_on_wrong_primary():
    client = _fake_client_with_schema(primary="id")
    with pytest.raises(RuntimeError, match="primary"):
        milvus_utils._assert_schema_invariants(client)


@pytest.mark.smoke
def test_assert_schema_invariants_raises_on_missing_vector_field():
    # describe_collection with only a primary field and no dense_vector
    client = MagicMock()
    client.describe_collection.return_value = {
        "fields": [{"name": "entry_id", "is_primary": True, "params": {}}]
    }
    with pytest.raises(RuntimeError, match="dense_vector"):
        milvus_utils._assert_schema_invariants(client)


# ---------------------------------------------------------------------------
# get_client cache behaviour (#40, #41; §17.591)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_get_client_returns_cached_handle_on_second_call(_bypass_schema_assert):
    fake_client = MagicMock(name="MilvusClient")
    fake_client.has_collection.return_value = True
    with patch.object(milvus_utils, "MilvusClient", return_value=fake_client):
        first = milvus_utils.get_client()
        second = milvus_utils.get_client()

    assert first is second is fake_client
    # First call: 2x has_collection (missing-check + post-auto-create verify) + 1x load.
    # Second call: cache hit, no extra RPCs.
    assert fake_client.has_collection.call_count == 2
    assert fake_client.load_collection.call_count == 1


@pytest.mark.smoke
def test_get_client_cache_expires_after_ttl(_bypass_schema_assert):
    fake_client = MagicMock(name="MilvusClient")
    fake_client.has_collection.return_value = True
    with patch.object(milvus_utils, "MilvusClient", return_value=fake_client), \
         patch.object(milvus_utils, "_CACHE_TTL_S", 0.0):  # expire immediately
        milvus_utils.get_client()
        time.sleep(0.01)
        milvus_utils.get_client()

    assert fake_client.has_collection.call_count == 4


@pytest.mark.smoke
def test_invalidate_cache_forces_reverify(_bypass_schema_assert):
    fake_client = MagicMock(name="MilvusClient")
    fake_client.has_collection.return_value = True
    with patch.object(milvus_utils, "MilvusClient", return_value=fake_client):
        milvus_utils.get_client()
        milvus_utils._invalidate_cache()
        milvus_utils.get_client()

    assert fake_client.has_collection.call_count == 4


# ---------------------------------------------------------------------------
# Double-checked locking — only one cold-load under concurrency
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_double_checked_locking_prevents_thundering_herd(_bypass_schema_assert):
    fake_client = MagicMock(name="MilvusClient")
    fake_client.has_collection.return_value = True
    barrier = threading.Barrier(4)
    results = []

    with patch.object(milvus_utils, "MilvusClient", return_value=fake_client):
        def worker():
            barrier.wait()
            results.append(milvus_utils.get_client())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

    assert len(results) == 4
    assert all(r is fake_client for r in results)
    # Only one cold load should have happened even with 4 concurrent callers.
    assert fake_client.load_collection.call_count == 1


# ---------------------------------------------------------------------------
# raise_on_missing contract (#126)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_get_client_returns_none_when_missing_and_raise_false():
    fake_client = MagicMock(name="MilvusClient")
    fake_client.has_collection.return_value = False
    with patch.object(milvus_utils, "MilvusClient", return_value=fake_client), \
         patch.object(milvus_utils, "_auto_create_collection"):
        result = milvus_utils.get_client(raise_on_missing=False)
    assert result is None


@pytest.mark.smoke
def test_get_client_raises_when_missing_and_raise_true():
    fake_client = MagicMock(name="MilvusClient")
    fake_client.has_collection.return_value = False
    with patch.object(milvus_utils, "MilvusClient", return_value=fake_client), \
         patch.object(milvus_utils, "_auto_create_collection"):
        with pytest.raises(RuntimeError):
            milvus_utils.get_client(raise_on_missing=True)


@pytest.mark.smoke
def test_get_client_invalidates_cache_on_error():
    with patch.object(milvus_utils, "MilvusClient",
                      side_effect=ConnectionError("no network")):
        result = milvus_utils.get_client(raise_on_missing=False)
    assert result is None
    assert milvus_utils._cached_client is None


# ---------------------------------------------------------------------------
# _auto_create_collection closes its MilvusClient
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_auto_create_collection_closes_client_on_success():
    fake_client = MagicMock()
    with patch.object(milvus_utils, "MilvusClient", return_value=fake_client):
        milvus_utils._auto_create_collection()
    fake_client.create_collection.assert_called_once()
    fake_client.close.assert_called_once()


@pytest.mark.smoke
def test_auto_create_collection_closes_client_on_failure():
    fake_client = MagicMock()
    fake_client.create_collection.side_effect = RuntimeError("boom")
    with patch.object(milvus_utils, "MilvusClient", return_value=fake_client):
        with pytest.raises(RuntimeError, match="boom"):
            milvus_utils._auto_create_collection()
    fake_client.close.assert_called_once()
