# tests/test_paper_daily_signal_proposal_batch.py
"""Tests for the daily signal-to-proposal batch entry points."""
from __future__ import annotations

import json
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_daily_signal_proposal_batch import (
    run_daily_batch,
    run_on_demand_batch,
)
from app.modules.paper_day_start_equity_recording import record_day_start_equity


async def _insert_ingested_item(
    db,
    *,
    source: str,
    ticker: str,
    published_ts: datetime,
) -> int:
    payload = {"ticker": ticker, "title": "test"}
    result = await db.execute(
        text(
            """
            INSERT INTO ingested_item
                (source, external_id, published_ts, payload)
            VALUES
                (:source, :external_id, :published_ts, CAST(:payload AS json))
            RETURNING id
            """
        ),
        {
            "source": source,
            "external_id": uuid4().hex,
            "published_ts": published_ts,
            "payload": json.dumps(payload),
        },
    )
    return result.scalar_one()


async def _record_equity(db, trading_day, equity: Decimal) -> int:
    return await record_day_start_equity(
        db=db,
        equity=equity,
        as_of_ts=datetime.combine(trading_day, time(13, 30), tzinfo=timezone.utc),
    )


@pytest.mark.asyncio
async def test_run_daily_batch_writes_proposal_and_risk_check():
    """A batch run with one signal writes one proposal and one risk check."""
    ts = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)
    trading_day = ts.date()

    # Mock the model call used by propose_trades for rationale.
    with patch(
        "app.modules.paper_trade_proposer.generate_json",
        new=AsyncMock(return_value=({"rationale": "test rationale"}, None)),
    ):
        async with async_session() as db:
            # Insert ingested item that will generate a signal.
            await _insert_ingested_item(
                db,
                source="news",
                ticker="SPY",
                published_ts=ts - timedelta(days=1),
            )
            await db.commit()

            # Record day-start equity.
            await _record_equity(db, trading_day, Decimal("100000"))
            await db.commit()

            result = await run_daily_batch(db=db, timestamp=ts)

    assert result["signals"] == 1
    assert result["proposals"] == 1
    assert result["proposals_written"] == 1
    assert result["risk_checks_written"] == 1
    assert len(result["results"]) == 1

    # Verify rows in database.
    async with async_session() as db:
        proposal_rows = await db.execute(text("SELECT * FROM proposal"))
        proposals = proposal_rows.fetchall()
        assert len(proposals) == 1
        proposal = proposals[0]
        assert proposal.ticker == "SPY"
        assert proposal.side == "buy"
        assert Decimal(str(proposal.quantity)) > 0

        risk_rows = await db.execute(text("SELECT * FROM risk_check"))
        risk_checks = risk_rows.fetchall()
        assert len(risk_checks) == 1
        risk = risk_checks[0]
        assert risk.proposal_id == proposal.id
        assert risk.check_type == "batch_deterministic"
        assert risk.passed is True


@pytest.mark.asyncio
async def test_run_daily_batch_no_signals_returns_zero_counts():
    """When no signals are generated, the batch writes nothing."""
    ts = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)
    trading_day = ts.date()

    async with async_session() as db:
        await _record_equity(db, trading_day, Decimal("100000"))
        await db.commit()

        result = await run_daily_batch(db=db, timestamp=ts)

    assert result == {
        "signals": 0,
        "proposals": 0,
        "proposals_written": 0,
        "risk_checks_written": 0,
        "results": [],
    }

    async with async_session() as db:
        proposal_count = await db.execute(text("SELECT COUNT(*) FROM proposal"))
        risk_count = await db.execute(text("SELECT COUNT(*) FROM risk_check"))
        assert proposal_count.scalar_one() == 0
        assert risk_count.scalar_one() == 0


@pytest.mark.asyncio
async def test_run_daily_batch_missing_equity_aborts():
    """If day-start equity is missing, the batch returns zero counts."""
    ts = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)

    async with async_session() as db:
        # No equity recorded.
        result = await run_daily_batch(db=db, timestamp=ts)

    assert result == {
        "signals": 0,
        "proposals": 0,
        "proposals_written": 0,
        "risk_checks_written": 0,
        "results": [],
    }


@pytest.mark.asyncio
async def test_run_on_demand_batch_same_as_daily():
    """run_on_demand_batch uses the same logic as run_daily_batch."""
    ts = datetime(2026, 1, 5, 14, 0, tzinfo=timezone.utc)
    trading_day = ts.date()

    with patch(
        "app.modules.paper_trade_proposer.generate_json",
        new=AsyncMock(return_value=({"rationale": "test rationale"}, None)),
    ):
        async with async_session() as db:
            await _insert_ingested_item(
                db,
                source="news",
                ticker="SPY",
                published_ts=ts - timedelta(days=1),
            )
            await db.commit()
            await _record_equity(db, trading_day, Decimal("100000"))
            await db.commit()

            result = await run_on_demand_batch(db=db, timestamp=ts)

    assert result["proposals_written"] == 1
    assert result["risk_checks_written"] == 1
