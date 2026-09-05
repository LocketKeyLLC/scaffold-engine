"""§17.947 — an unreachable Milvus must not look like an empty one.

The helper used to swallow every exception and return 0, so a service that was
DOWN was indistinguishable from a collection that was EMPTY. §17.946 blocked
Milvus in the unit lane and the suite reported 0 failures with 8 skips instead
of 1: seven live-retrieval parametrizations had stopped existing rather than
failing, and only a skip-count comparison caught it.

The two conditions deserve opposite outcomes:
  * empty       → a data condition; skip, there is nothing to retrieve
  * unreachable → an environment failure; FAIL, the test could not even ask
"""
from unittest.mock import MagicMock, patch

import pytest
from _pytest.outcomes import Failed

from tests import _milvus_helpers as mh
from tests._milvus_helpers import (
    MilvusUnavailable,
    get_collection_entry_count,
    skip_if_milvus_empty,
)


def _client(*, collections=("toon_v2",), rows=0):
    c = MagicMock()
    c.list_collections.return_value = list(collections)
    c.get_collection_stats.return_value = {"row_count": rows}
    return c


# ── the three conditions, kept apart ──────────────────────────────────────


def test_populated_collection_returns_its_row_count():
    with patch("pymilvus.MilvusClient", return_value=_client(rows=3752)):
        assert get_collection_entry_count("toon_v2") == 3752


def test_empty_collection_is_zero_not_an_error():
    """A wiped corpus is a data condition — the §17.63 case this was built
    for."""
    with patch("pymilvus.MilvusClient", return_value=_client(rows=0)):
        assert get_collection_entry_count("toon_v2") == 0


def test_a_missing_collection_is_also_zero():
    """From a retrieval test's point of view "absent" and "empty" are the same
    data condition — there is nothing to retrieve either way."""
    with patch("pymilvus.MilvusClient", return_value=_client(collections=())):
        assert get_collection_entry_count("toon_v2") == 0


def test_a_refused_connection_raises_rather_than_returning_zero():
    """THE regression. Returning 0 here is what made a dead service look like
    an empty corpus."""
    with patch("pymilvus.MilvusClient", side_effect=ConnectionError("refused")):
        with pytest.raises(MilvusUnavailable) as exc:
            get_collection_entry_count("toon_v2")
    assert "cannot connect" in str(exc.value)


def test_a_dead_server_mid_query_also_raises():
    """Constructing the client can succeed against a server that then dies —
    the failure must still be UNAVAILABLE, not zero rows."""
    c = _client()
    c.list_collections.side_effect = RuntimeError("connection reset")
    with patch("pymilvus.MilvusClient", return_value=c):
        with pytest.raises(MilvusUnavailable):
            get_collection_entry_count("toon_v2")


# ── what the callers actually see ─────────────────────────────────────────


def test_empty_collection_skips():
    with patch("pymilvus.MilvusClient", return_value=_client(rows=0)):
        with pytest.raises(pytest.skip.Exception) as exc:
            skip_if_milvus_empty("toon_v2")
    assert "is empty" in str(exc.value)


def test_unreachable_fails_by_default():
    """§17.947 — a test that needs a service it cannot reach is broken, not
    inapplicable. This is the behaviour that would have surfaced §17.946
    immediately instead of via a skip-count comparison."""
    with patch("pymilvus.MilvusClient", side_effect=ConnectionError("refused")):
        with pytest.raises(Failed) as exc:
            skip_if_milvus_empty("toon_v2")
    msg = str(exc.value)
    assert "UNREACHABLE" in msg
    assert "not an empty corpus" in msg
    assert "integration" in msg          # names the likely fix


def test_unreachable_can_be_tolerated_but_says_so():
    """Opting out still must not call it 'empty'."""
    with patch("pymilvus.MilvusClient", side_effect=ConnectionError("refused")):
        with pytest.raises(pytest.skip.Exception) as exc:
            skip_if_milvus_empty("toon_v2", require_reachable=False)
    msg = str(exc.value)
    assert "UNREACHABLE" in msg
    assert "empty" not in msg.replace("(not empty)", "")


def test_a_populated_collection_neither_skips_nor_fails():
    with patch("pymilvus.MilvusClient", return_value=_client(rows=3752)):
        skip_if_milvus_empty("toon_v2")   # must simply return
