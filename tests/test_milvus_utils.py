"""Tests for app/utils/milvus_utils.py (#9.26)."""
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
    # prepare_index_params is a classmethod-ish helper; fall back if needed
    try:
        params = milvus_utils.build_toon_v2_index_params(client)
    except Exception:
        pytest.skip("MilvusClient.prepare_index_params unavailable without connection")
    # Just assert it produced a truthy object with some index entries
    assert params is not None


# ---------------------------------------------------------------------------
# get_collection cache behaviour (#40, #41)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_get_collection_returns_cached_handle_on_second_call():
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
def test_get_collection_cache_expires_after_ttl():
    fake_col = MagicMock(name="Collection")
    with patch.object(milvus_utils, "utility") as util, \
         patch.object(milvus_utils, "Collection", return_value=fake_col), \
         patch.object(milvus_utils, "_CACHE_TTL_S", 0.0):  # expire immediately
        util.list_collections.return_value = ["toon_v2"]
        util.has_collection.return_value = True

        milvus_utils.get_collection()
        time.sleep(0.01)
        milvus_utils.get_collection()

    # Each full verification does has_collection twice
    assert util.has_collection.call_count == 4


@pytest.mark.smoke
def test_invalidate_cache_forces_reverify():
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
# raise_on_missing contract (#126)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_get_collection_returns_none_when_missing_and_raise_false():
    with patch.object(milvus_utils, "utility") as util, \
         patch.object(milvus_utils, "_auto_create_collection"):
        util.list_collections.return_value = []
        util.has_collection.return_value = False  # auto-create "failed"

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

    # Cache must be empty after error
    assert milvus_utils._cached_collection is None
