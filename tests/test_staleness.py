"""Tests for app/utils/staleness.py (#9.27)."""
from unittest.mock import MagicMock, patch

import pytest

from app.utils import staleness


# ---------------------------------------------------------------------------
# TTL policy (#131, #133)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_ttl_policy_known_source_types():
    assert staleness.get_ttl_for_source("real_time") == 7 * 86400
    assert staleness.get_ttl_for_source("news") == 30 * 86400
    assert staleness.get_ttl_for_source("tech_docs") == 180 * 86400
    assert staleness.get_ttl_for_source("curated") == 365 * 86400


@pytest.mark.smoke
def test_unknown_source_type_falls_back_to_default_and_warns(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="scaffold.staleness"):
        ttl = staleness.get_ttl_for_source("totally_unknown")
    assert ttl == staleness.DEFAULT_TTL_SECONDS
    assert any("staleness_unknown_source_type" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# compute_expires_at (#132)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
def test_compute_expires_at_uses_now_when_created_at_is_none():
    import time as _time
    before = int(_time.time())
    result = staleness.compute_expires_at("news")
    after = int(_time.time())
    # result = now + 30 days, within a 2s window
    assert before + 30 * 86400 <= result <= after + 30 * 86400


@pytest.mark.smoke
def test_compute_expires_at_respects_explicit_created_at():
    result = staleness.compute_expires_at("news", created_at=1_000_000)
    assert result == 1_000_000 + 30 * 86400


@pytest.mark.smoke
def test_compute_expires_at_zero_is_treated_as_explicit_not_sentinel():
    # #132: 0 is a legitimate epoch value now, not a "use now" sentinel
    result = staleness.compute_expires_at("news", created_at=0)
    assert result == 0 + 30 * 86400


# ---------------------------------------------------------------------------
# sweep_expired (#48, #49, #134)
# ---------------------------------------------------------------------------
@pytest.mark.smoke
@pytest.mark.asyncio
async def test_sweep_expired_returns_ok_on_empty():
    fake_col = MagicMock()
    fake_col.query.return_value = []
    with patch.object(staleness, "get_client", return_value=fake_col):
        result = await staleness.sweep_expired()
    assert result["status"] == "ok"
    assert result["expired_count"] == 0
    assert result["deleted"] == []


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_sweep_expired_deletes_and_returns_titles():
    fake_col = MagicMock()
    # First page: 3 entries. Second page: empty -> loop stops.
    fake_col.query.side_effect = [
        [
            {"entry_id": "a", "title": "Alpha"},
            {"entry_id": "b", "title": "Beta"},
            {"entry_id": "c", "title": "Gamma"},
        ],
        [],
    ]
    with patch.object(staleness, "get_client", return_value=fake_col):
        result = await staleness.sweep_expired()

    assert result["status"] == "ok"
    assert result["expired_count"] == 3
    assert set(result["deleted"]) == {"Alpha", "Beta", "Gamma"}
    assert result["deleted_truncated"] is False
    # Delete expression should use quoted IDs (§17.591 — MilvusClient filter= kwarg)
    assert 'entry_id in [' in fake_col.delete.call_args.kwargs["filter"]


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_sweep_expired_paginates():
    fake_col = MagicMock()
    # Page 1: full 1000. Page 2: 5 entries (< PAGE_SIZE -> stop).
    page1 = [{"entry_id": f"id{i}", "title": f"T{i}"} for i in range(1000)]
    page2 = [{"entry_id": f"id{i}", "title": f"T{i}"} for i in range(1000, 1005)]
    fake_col.query.side_effect = [page1, page2]
    with patch.object(staleness, "get_client", return_value=fake_col):
        result = await staleness.sweep_expired()
    assert result["expired_count"] == 1005
    assert fake_col.query.call_count == 2
    assert fake_col.delete.call_count == 2


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_sweep_expired_no_max_id_cursor():
    """§17.604 — must NOT paginate via a max(entry_id) cursor. client.query()
    is unordered, so `entry_id > max(ids)` skipped lower expired ids; the sweep
    now deletes each batch + dedups via a seen-set. Assert no query filter
    carries an entry_id-comparison cursor and both ids are deleted regardless
    of order ('a' < 'z' would have been skipped by a max(ids)='z' cursor)."""
    fake_col = MagicMock()
    fake_col.query.side_effect = [
        [{"entry_id": "z", "title": "Z"}, {"entry_id": "a", "title": "A"}],
        [],
    ]
    with patch.object(staleness, "get_client", return_value=fake_col):
        result = await staleness.sweep_expired()
    assert set(result["deleted"]) == {"Z", "A"}
    for call in fake_col.query.call_args_list:
        assert "entry_id >" not in call.kwargs.get("filter", "")


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_sweep_expired_caps_titles_at_50():
    fake_col = MagicMock()
    entries = [{"entry_id": f"id{i}", "title": f"T{i}"} for i in range(120)]
    fake_col.query.side_effect = [entries, []]
    with patch.object(staleness, "get_client", return_value=fake_col):
        result = await staleness.sweep_expired()
    assert result["expired_count"] == 120
    assert len(result["deleted"]) == 50
    assert result["deleted_truncated"] is True


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_sweep_expired_escapes_quotes_in_ids():
    fake_col = MagicMock()
    fake_col.query.side_effect = [
        [{"entry_id": 'has"quote', "title": "Weird"}],
        [],
    ]
    with patch.object(staleness, "get_client", return_value=fake_col):
        await staleness.sweep_expired()
    # The delete expression must escape the inner quote so Milvus can parse it
    # (§17.591 — MilvusClient filter= kwarg)
    expr = fake_col.delete.call_args.kwargs["filter"]
    assert r'\"' in expr


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_sweep_expired_returns_error_when_collection_unavailable():
    with patch.object(staleness, "get_client", return_value=None):
        result = await staleness.sweep_expired()
    assert result["status"] == "error"
