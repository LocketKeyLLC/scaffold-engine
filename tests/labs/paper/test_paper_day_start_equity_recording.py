# tests/test_paper_day_start_equity_recording.py
"""Tests for the day-start equity recording module."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_day_start_equity_recording import (
    get_day_start_equity,
    record_day_start_equity,
)


def _unique_trading_day() -> date:
    n = int(uuid.uuid4().hex[:8], 16) % 100000
    return date(2020, 1, 1) + timedelta(days=n)


def _day_start_ts(trading_day: date) -> datetime:
    return datetime.combine(trading_day, time(13, 30), tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_record_and_read_day_start_equity():
    trading_day = _unique_trading_day()
    equity = Decimal("100000.00")

    async with async_session() as db:
        row_id = await record_day_start_equity(
            db=db,
            equity=equity,
            as_of_ts=_day_start_ts(trading_day),
        )
        assert row_id > 0

        got = await get_day_start_equity(db=db, trading_day=trading_day)
        assert got == equity


@pytest.mark.asyncio
async def test_record_is_idempotent_per_utc_date():
    trading_day = _unique_trading_day()
    equity = Decimal("100000.00")

    async with async_session() as db:
        first_id = await record_day_start_equity(
            db=db,
            equity=equity,
            as_of_ts=_day_start_ts(trading_day),
        )
        second_id = await record_day_start_equity(
            db=db,
            equity=Decimal("99999.00"),
            as_of_ts=_day_start_ts(trading_day) + timedelta(hours=1),
        )
        assert second_id == first_id

        got = await get_day_start_equity(db=db, trading_day=trading_day)
        assert got == equity


@pytest.mark.asyncio
async def test_get_returns_none_when_no_record():
    trading_day = _unique_trading_day()

    async with async_session() as db:
        got = await get_day_start_equity(db=db, trading_day=trading_day)
        assert got is None


@pytest.mark.asyncio
async def test_record_rejects_non_positive_equity():
    async with async_session() as db:
        with pytest.raises(ValueError, match="equity must be positive"):
            await record_day_start_equity(
                db=db,
                equity=Decimal("0"),
                as_of_ts=_day_start_ts(_unique_trading_day()),
            )


@pytest.mark.asyncio
async def test_record_defaults_cash_and_account_value_to_equity():
    trading_day = _unique_trading_day()
    equity = Decimal("50000.00")

    async with async_session() as db:
        row_id = await record_day_start_equity(
            db=db,
            equity=equity,
            as_of_ts=_day_start_ts(trading_day),
        )
        result = await db.execute(
            text("SELECT cash, account_value FROM pnl WHERE id = :id"),
            {"id": row_id},
        )
        row = result.one()
        assert Decimal(str(row.cash)) == equity
        assert Decimal(str(row.account_value)) == equity


@pytest.mark.asyncio
async def test_record_uses_provided_cash_and_account_value():
    trading_day = _unique_trading_day()
    equity = Decimal("50000.00")
    cash = Decimal("30000.00")
    account_value = Decimal("52000.00")

    async with async_session() as db:
        row_id = await record_day_start_equity(
            db=db,
            equity=equity,
            cash=cash,
            account_value=account_value,
            as_of_ts=_day_start_ts(trading_day),
        )
        result = await db.execute(
            text("SELECT cash, account_value FROM pnl WHERE id = :id"),
            {"id": row_id},
        )
        row = result.one()
        assert Decimal(str(row.cash)) == cash
        assert Decimal(str(row.account_value)) == account_value


@pytest.mark.asyncio
async def test_get_picks_earliest_row_of_the_day():
    trading_day = _unique_trading_day()
    day_start_equity = Decimal("100000.00")
    later_equity = Decimal("99000.00")

    async with async_session() as db:
        await record_day_start_equity(
            db=db,
            equity=day_start_equity,
            as_of_ts=_day_start_ts(trading_day),
        )
        await db.execute(
            text(
                """
                INSERT INTO pnl (as_of_ts, account_value, cash, equity)
                VALUES (:as_of_ts, :account_value, :cash, :equity)
                """
            ),
            {
                "as_of_ts": _day_start_ts(trading_day) + timedelta(hours=8),
                "account_value": later_equity,
                "cash": later_equity,
                "equity": later_equity,
            },
        )
        await db.commit()

        got = await get_day_start_equity(db=db, trading_day=trading_day)
        assert got == day_start_equity


@pytest.mark.asyncio
async def test_naive_as_of_ts_gets_utc_tzinfo():
    trading_day = _unique_trading_day()
    equity = Decimal("100000.00")
    naive_ts = datetime.combine(trading_day, time(13, 30))

    async with async_session() as db:
        row_id = await record_day_start_equity(
            db=db,
            equity=equity,
            as_of_ts=naive_ts,
        )
        result = await db.execute(
            text("SELECT as_of_ts FROM pnl WHERE id = :id"),
            {"id": row_id},
        )
        stored = result.scalar_one()
        assert stored.utcoffset() == timedelta(0)
