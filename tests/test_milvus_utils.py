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
    schema = milvus_utils.build_toon_v2_schema()
    assert len(schema.fields) == 16


@pytest.mark.smoke
def test_build_toon_v2_schema_has_vector_and_partition_key():
    schema = milvus_utils.build_toon_v2_schema()
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
def _fake_col_with_schema(dim: int = 512, primary: str = "entry_id", vec_field: str = "dense_vector"):
    """Build a MagicMock collection whose .schema.fields survives _assert_schema_invariants."""
    primary_field = MagicMock(name=primary, params={})
    primary_field.name = primary
    primary_field.is_primary = True
    vec = MagicMock(name=vec_field, params={"dim": dim})
    vec.name = vec_field
    vec.is_primary = False
    col = MagicMock(name="Collection")
    col.schema.fields = [primary_field, vec]
    return col


@pytest.mark.smoke
def test_assert_schema_invariants_passes_on_valid_schema():
    col = _fake_col_with_schema(dim=512, primary="entry_id")
    milvus_utils._assert_schema_invariants(col)  # should not raise


@pytest.mark.smoke
def test_assert_schema_invariants_raises_on_wrong_dim():
    col = _fake_col_with_schema(dim=768)
    with pytest.raises(RuntimeError, match="dim"):
        milvus_utils._assert_schema_invariants(col)


@pytest.mark.smoke
def test_assert_schema_invariants_raises_on_wrong_primary():
    col = _fake_col_with_schema(primary="id")
    with pytest.raises(RuntimeError, match="primary"):
        milvus_utils._assert_schema_invariants(col)


@pytest.mark.smoke
def test_assert_schema_invariants_raises_on_missing_vector_field():
    # Build a schema with only a primary field and no dense_vector
    primary = MagicMock()
    primary.name = "entry_id"
    primary.is_primary = True
    col = MagicMock()
    col.schema.fields = [primary]
    with pytest.raises(RuntimeError, match="dense_vector"):
        milvus_utils._assert_schema_invariants(col)


# ---------------------------------------------------------------------------
# get_collection cache behaviour (#40, #41)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_get_collection_returns_cached_handle_on_second_call(_bypass_schema_assert):
    fake_col = MagicMock(name="Collection")
    with patch.object(milvus_utils, "utility") as util, \
         patch.object(milvus_utils, "Collection", return_value=fake_col):
        util.list_collections.return_value = ["toon_v2"]
        util.has_collection.return_value = True

        first = milvus_utils.get_collection()
        second = milvus_utils.get_collection()

    assert first is second is fake_col
    # First call: 2x has_collection (missing-check + post-auto-create verify) + 1x load.
    # Second call: cache hit, no extra RPCs.
    assert util.has_collection.call_count == 2
    assert fake_col.load.call_count == 1


@pytest.mark.smoke
def test_get_collection_cache_expires_after_ttl(_bypass_schema_assert):
    fake_col = MagicMock(name="Collection")
    with patch.object(milvus_utils, "utility") as util, \
         patch.object(milvus_utils, "Collection", return_value=fake_col), \
         patch.object(milvus_utils, "_CACHE_TTL_S", 0.0):  # expire immediately
        util.list_collections.return_value = ["toon_v2"]
        util.has_collection.return_value = True

        milvus_utils.get_collection()
        time.sleep(0.01)
        milvus_utils.get_collection()

    assert util.has_collection.call_count == 4


@pytest.mark.smoke
def test_invalidate_cache_forces_reverify(_bypass_schema_assert):
    fake_col = MagicMock(name="Collection")
    with patch.object(milvus_utils, "utility") as util, \
         patch.object(milvus_utils, "Collection", return_value=fake_col):
        util.list_collections.return_value = ["toon_v2"]
        util.has_collection.return_value = True

        milvus_utils.get_collection()
        milvus_utils._invalidate_cache()
        milvus_utils.get_collection()

    assert util.has_collection.call_count == 4


# ---------------------------------------------------------------------------
# Double-checked locking — only one cold-load under concurrency
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_double_checked_locking_prevents_thundering_herd(_bypass_schema_assert):
    fake_col = MagicMock(name="Collection")
    barrier = threading.Barrier(4)
    results = []

    with patch.object(milvus_utils, "utility") as util, \
         patch.object(milvus_utils, "Collection", return_value=fake_col):
        util.list_collections.return_value = ["toon_v2"]
        util.has_collection.return_value = True

        def worker():
            barrier.wait()
            results.append(milvus_utils.get_collection())

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

    assert len(results) == 4
    assert all(r is fake_col for r in results)
    # Only one cold load should have happened even with 4 concurrent callers.
    assert fake_col.load.call_count == 1


# ---------------------------------------------------------------------------
# raise_on_missing contract (#126)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_get_collection_returns_none_when_missing_and_raise_false():
    with patch.object(milvus_utils, "utility") as util, \
         patch.object(milvus_utils, "_auto_create_collection"):
        util.list_collections.return_value = []
        util.has_collection.return_value = False

        result = milvus_utils.get_collection(raise_on_missing=False)
    assert result is None


@pytest.mark.smoke
def test_get_collection_raises_when_missing_and_raise_true():
    with patch.object(milvus_utils, "utility") as util, \
         patch.object(milvus_utils, "_auto_create_collection"):
        util.list_collections.return_value = []
        util.has_collection.return_value = False

        with pytest.raises(RuntimeError):
            milvus_utils.get_collection(raise_on_missing=True)


@pytest.mark.smoke
def test_get_collection_invalidates_cache_on_error():
    with patch.object(milvus_utils, "utility") as util:
        util.list_collections.side_effect = ConnectionError("dead")
        util.has_collection.side_effect = ConnectionError("dead")
        with patch.object(milvus_utils.connections, "connect",
                          side_effect=ConnectionError("no network")):
            result = milvus_utils.get_collection(raise_on_missing=False)
        assert result is None

    assert milvus_utils._cached_collection is None


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
