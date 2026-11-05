# tests/test_paper_entry_backtesting.py
"""Tests for the paper entry backtesting module."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_entry_backtesting import (
    _latest_data_timestamp,
    run_backtesting_entry_point,
)


def _ts(days_ago: int = 0) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days_ago)


async def _insert_ingested_item(
    db: object,
    *,
    published_ts: datetime,
    source: str = "news",
    external_id: str = "ext-1",
) -> None:
    await db.execute(
        text(
            """
            INSERT INTO ingested_item
                (external_id, source, published_ts, fetched_ts, payload)
            VALUES
                (:external_id, :source, :published_ts, :fetched_ts,
                 CAST(:payload AS json))
            """
        ),
        {
            "external_id": external_id,
            "source": source,
            "published_ts": published_ts,
            "fetched_ts": published_ts,
            "payload": '{"headline": "test"}',
        },
    )
    await db.commit()


@pytest.mark.asyncio
async def test_latest_data_timestamp_empty_returns_none():
    async with async_session() as db:
        result = await _latest_data_timestamp(db)
        assert result is None


@pytest.mark.asyncio
async def test_latest_data_timestamp_returns_max():
    async with async_session() as db:
        older = _ts(days_ago=10)
        newer = _ts(days_ago=2)
        await _insert_ingested_item(db, published_ts=older, external_id="ext-old")
        await _insert_ingested_item(db, published_ts=newer, external_id="ext-new")

        result = await _latest_data_timestamp(db)
        assert result is not None
        assert result == newer


@pytest.mark.asyncio
async def test_run_backtesting_entry_point_with_data():
    latest = _ts(days_ago=1)
    async with async_session() as db:
        await _insert_ingested_item(db, published_ts=latest)

        with patch(
            "app.modules.paper_entry_backtesting.run_backtest",
            new=AsyncMock(return_value={"total_return": 0.05}),
        ) as mock_backtest:
            result = await run_backtesting_entry_point(db=db)

        assert result == {"total_return": 0.05}
        mock_backtest.assert_awaited_once()

        call_kwargs = mock_backtest.await_args.kwargs
        assert "start_date" in call_kwargs
        assert "end_date" in call_kwargs
        assert call_kwargs["end_date"] == latest
        assert call_kwargs["start_date"] == latest - timedelta(days=60)


@pytest.mark.asyncio
async def test_run_backtesting_entry_point_empty_database():
    async with async_session() as db:
        with patch(
            "app.modules.paper_entry_backtesting.run_backtest",
            new=AsyncMock(return_value={"total_return": 0.0}),
        ) as mock_backtest:
            result = await run_backtesting_entry_point(db=db)

        assert result == {"total_return": 0.0}
        mock_backtest.assert_awaited_once()

        call_kwargs = mock_backtest.await_args.kwargs
        assert "start_date" in call_kwargs
        assert "end_date" in call_kwargs
        assert isinstance(call_kwargs["end_date"], datetime)
        assert call_kwargs["end_date"] > call_kwargs["start_date"]
        assert call_kwargs["db"] is db
