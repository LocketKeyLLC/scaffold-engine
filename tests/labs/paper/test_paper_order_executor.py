# tests/test_paper_order_executor.py
"""Tests for app.modules.paper_order_executor."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_alpaca_paper_client import (
    AlpacaOrder,
    AlpacaPaperClientError,
)
from app.modules.paper_audit_trail_writer import (
    write_approval,
    write_proposal,
    write_signal,
)
from app.modules.paper_order_executor import execute_order
from sqlalchemy.exc import IntegrityError


async def _insert_ingested_item() -> int:
    """Insert a minimal ingested_item row and return its id."""
    async with async_session() as db:
        external_id = uuid4().hex
        payload = json.dumps({"ticker": "AAPL"})
        result = await db.execute(
            text(
                """
                INSERT INTO ingested_item (source, external_id, published_ts, payload)
                VALUES (:source, :external_id, :published_ts, CAST(:payload AS json))
                RETURNING id
                """
            ),
            {
                "source": "finra",
                "external_id": external_id,
                "published_ts": datetime.now(timezone.utc),
                "payload": payload,
            },
        )
        ingested_id = result.fetchone()[0]
        await db.commit()
        return ingested_id


async def _create_signal_proposal_approval(
    order_type: str = "market",
    limit_price: str | None = None,
) -> tuple[int, int]:
    """Create an approved proposal and return (approval_id, proposal_id)."""
    ingested_id = await _insert_ingested_item()
    signal_id = await write_signal(
        ingested_item_id=ingested_id,
        ticker="AAPL",
        signal_type="finra_context",
        direction="long",
        rationale="test signal",
    )
    proposal_id = await write_proposal(
        signal_id=signal_id,
        ticker="AAPL",
        side="buy",
        quantity="1",
        order_type=order_type,
        limit_price=limit_price,
        rationale="test proposal",
    )
    approval_id = await write_approval(
        proposal_id=proposal_id,
        operator_id="test_operator",
        decision="approved",
        reason=None,
    )
    return approval_id, proposal_id


@pytest.mark.asyncio
async def test_execute_order_missing_keys():
    with pytest.raises(KeyError):
        await execute_order({"id": 1, "decision": "approved"})  # missing proposal_id


@pytest.mark.asyncio
async def test_execute_order_proposal_not_found():
    with pytest.raises(ValueError):
        await execute_order(
            {"id": 999, "proposal_id": 999, "decision": "approved"}
        )


@pytest.mark.asyncio
async def test_execute_order_success():
    approval_id, proposal_id = await _create_signal_proposal_approval("market")
    fake_order = AlpacaOrder(
        id="broker-123",
        client_order_id=None,
        symbol="AAPL",
        side="buy",
        order_type="market",
        qty=Decimal("1"),
        time_in_force="day",
        limit_price=None,
        stop_price=None,
        status="accepted",
        created_at=datetime.now(timezone.utc),
        submitted_at=None,
        filled_at=None,
        filled_qty=Decimal("0"),
        filled_avg_price=None,
    )

    with patch("app.modules.paper_order_executor.AlpacaPaperClient") as mock_cls:
        mock_cls.return_value.submit_order = AsyncMock(return_value=fake_order)
        mock_cls.return_value.close = AsyncMock()
        result = await execute_order(
            {"id": approval_id, "proposal_id": proposal_id, "decision": "approved"}
        )

    assert result["status"] == "submitted"
    assert result["proposal_id"] == proposal_id
    assert result["order"] is fake_order

    # Verify broker_order_id row was written by the real write_order.
    async with async_session() as db:
        row = await db.execute(
            text(
                """
                SELECT id, broker_order_id
                FROM broker_order_id
                WHERE approval_id = :aid AND proposal_id = :pid
                """
            ),
            {"aid": approval_id, "pid": proposal_id},
        )
        rows = row.fetchall()
        assert len(rows) == 1
        db_id, broker_id = rows[0]
        assert broker_id == "broker-123"
        assert result["broker_order_id"] == db_id


@pytest.mark.asyncio
async def test_execute_order_client_error():
    approval_id, proposal_id = await _create_signal_proposal_approval("market")

    with patch("app.modules.paper_order_executor.AlpacaPaperClient") as mock_cls:
        mock_cls.return_value.submit_order = AsyncMock(
            side_effect=AlpacaPaperClientError("API down")
        )
        mock_cls.return_value.close = AsyncMock()
        with pytest.raises(AlpacaPaperClientError):
            await execute_order(
                {"id": approval_id, "proposal_id": proposal_id, "decision": "approved"}
            )

    # No broker_order_id row should be written.
    async with async_session() as db:
        row = await db.execute(
            text(
                """
                SELECT COUNT(*)
                FROM broker_order_id
                WHERE approval_id = :aid AND proposal_id = :pid
                """
            ),
            {"aid": approval_id, "pid": proposal_id},
        )
        count = row.fetchone()[0]
        assert count == 0


@pytest.mark.asyncio
async def test_execute_order_audit_writer_error():
    approval_id, proposal_id = await _create_signal_proposal_approval("market")

    # Pre-insert a broker_order_id row so the real write_order hits a unique
    # constraint on approval_id. This is the real failure path, not a mock.
    async with async_session() as db:
        await db.execute(
            text(
                """
                INSERT INTO broker_order_id (approval_id, proposal_id, broker_order_id)
                VALUES (:aid, :pid, :bid)
                """
            ),
            {"aid": approval_id, "pid": proposal_id, "bid": "existing-123"},
        )
        await db.commit()

    fake_order = AlpacaOrder(
        id="broker-456",
        client_order_id=None,
        symbol="AAPL",
        side="buy",
        order_type="market",
        qty=Decimal("1"),
        time_in_force="day",
        limit_price=None,
        stop_price=None,
        status="accepted",
        created_at=datetime.now(timezone.utc),
        submitted_at=None,
        filled_at=None,
        filled_qty=Decimal("0"),
        filled_avg_price=None,
    )

    with patch("app.modules.paper_order_executor.AlpacaPaperClient") as mock_cls:
        mock_cls.return_value.submit_order = AsyncMock(return_value=fake_order)
        mock_cls.return_value.close = AsyncMock()

        with pytest.raises(IntegrityError):
            await execute_order(
                {"id": approval_id, "proposal_id": proposal_id, "decision": "approved"}
            )

    # The conflicting row is still the only one; write_order did not add another.
    async with async_session() as db:
        row = await db.execute(
            text(
                """
                SELECT COUNT(*)
                FROM broker_order_id
                WHERE approval_id = :aid AND proposal_id = :pid
                """
            ),
            {"aid": approval_id, "pid": proposal_id},
        )
        count = row.fetchone()[0]
        assert count == 1


@pytest.mark.asyncio
async def test_execute_order_limit_order():
    approval_id, proposal_id = await _create_signal_proposal_approval(
        "limit", "150.00"
    )
    fake_order = AlpacaOrder(
        id="broker-789",
        client_order_id=None,
        symbol="AAPL",
        side="buy",
        order_type="limit",
        qty=Decimal("1"),
        time_in_force="day",
        limit_price=Decimal("150.00"),
        stop_price=None,
        status="accepted",
        created_at=datetime.now(timezone.utc),
        submitted_at=None,
        filled_at=None,
        filled_qty=Decimal("0"),
        filled_avg_price=None,
    )

    with patch("app.modules.paper_order_executor.AlpacaPaperClient") as mock_cls:
        mock_submit = AsyncMock(return_value=fake_order)
        mock_cls.return_value.submit_order = mock_submit
        mock_cls.return_value.close = AsyncMock()

        await execute_order(
            {"id": approval_id, "proposal_id": proposal_id, "decision": "approved"}
        )

        assert mock_submit.await_count == 1
        call_kwargs = mock_submit.await_args.kwargs
        assert call_kwargs["symbol"] == "AAPL"
        assert call_kwargs["qty"] == Decimal("1")
        assert call_kwargs["side"] == "buy"
        assert call_kwargs["order_type"] == "limit"
        assert call_kwargs["limit_price"] == Decimal("150.00")
        assert call_kwargs["time_in_force"] == "day"
