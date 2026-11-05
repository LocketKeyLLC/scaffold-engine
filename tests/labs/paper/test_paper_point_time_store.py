# tests/test_paper_point_time_store.py
"""Tests for app.modules.paper_point_time_store."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from app.modules.paper_point_time_store import get_data_as_of


class FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class FakeDB:
    def __init__(self, rows):
        self.rows = rows
        self.executed_query = None
        self.executed_params = None

    async def execute(self, query, params):
        self.executed_query = query
        self.executed_params = params
        return FakeResult(self.rows)


@pytest.mark.asyncio
async def test_get_data_as_of_uses_inclusive_boundary():
    ts = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    rows = [({"ticker": "AAPL"},)]
    db = FakeDB(rows)

    result = await get_data_as_of(ts, db=db)

    assert result == [{"ticker": "AAPL"}]
    assert "published_ts <= :ts" in str(db.executed_query)
    assert db.executed_params == {"ts": ts}


@pytest.mark.asyncio
async def test_get_data_as_of_empty_when_no_rows():
    ts = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    db = FakeDB([])

    result = await get_data_as_of(ts, db=db)

    assert result == []


@pytest.mark.asyncio
async def test_get_data_as_of_payload_is_dict():
    ts = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    payload = {"ticker": "SPY", "price": 500.0}
    db = FakeDB([(payload,)])

    result = await get_data_as_of(ts, db=db)

    assert len(result) == 1
    assert isinstance(result[0], dict)
    assert result[0] == payload


@pytest.mark.asyncio
async def test_get_data_as_of_default_session():
    ts = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    fake_session = AsyncMock()
    fake_session.__aenter__.return_value = FakeDB([({"x": 1},)])
    fake_session.__aexit__.return_value = AsyncMock()

    with patch("app.modules.paper_point_time_store.async_session", return_value=fake_session):
        result = await get_data_as_of(ts)

    assert result == [{"x": 1}]


@pytest.mark.asyncio
async def test_get_data_as_of_propagates_db_error():
    ts = datetime(2026, 10, 24, 12, 0, tzinfo=timezone.utc)
    db = FakeDB([])
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))

    with pytest.raises(RuntimeError):
        await get_data_as_of(ts, db=db)
