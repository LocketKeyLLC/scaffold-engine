# tests/test_paper_execution_entry_point.py
"""Tests for app.modules.paper_execution_entry_point."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_execution_entry_point import (
    PAPER_BASE_URL,
    _send_order,
    run_execution_entry_point,
)


# ---------------------------------------------------------------------------
# Insert helpers (each inserts one row into the scratch database and returns
# its id; a caller commits before running the code under test).
# ---------------------------------------------------------------------------

async def _insert_ingested_item(
    db,
    *,
    source: str = "news",
    external_id: str | None = None,
    published_ts: datetime | None = None,
    payload: dict | None = None,
) -> int:
    external_id = external_id or uuid4().hex
    published_ts = published_ts or datetime.now(timezone.utc)
    payload = payload or {}
    result = await db.execute(
        text(
            "INSERT INTO ingested_item "
            "(source, external_id, published_ts, payload, data_ts, model_ts, fetched_ts) "
            "VALUES (:source, :external_id, :published_ts, CAST(:payload AS json), "
            ":data_ts, :model_ts, :fetched_ts) RETURNING id"
        ),
        {
            "source": source,
            "external_id": external_id,
            "published_ts": published_ts,
            "payload": json.dumps(payload),
            "data_ts": published_ts,
            "model_ts": published_ts,
            "fetched_ts": published_ts,
        },
    )
    return result.scalar_one()


async def _insert_signal(
    db,
    ingested_item_id: int,
    *,
    ticker: str = "SPY",
    signal_type: str = "news_context",
    direction: str = "long",
    rationale: str = "test rationale",
) -> int:
    result = await db.execute(
        text(
            "INSERT INTO signal "
            "(ingested_item_id, ticker, signal_type, direction, rationale, data_ts, model_ts) "
            "VALUES (:ingested_item_id, :ticker, :signal_type, :direction, :rationale, "
            ":data_ts, :model_ts) RETURNING id"
        ),
        {
            "ingested_item_id": ingested_item_id,
            "ticker": ticker,
            "signal_type": signal_type,
            "direction": direction,
            "rationale": rationale,
            "data_ts": datetime.now(timezone.utc),
            "model_ts": datetime.now(timezone.utc),
        },
    )
    return result.scalar_one()


async def _insert_proposal(
    db,
    signal_id: int,
    *,
    ticker: str = "SPY",
    side: str = "buy",
    quantity: str = "1",
    order_type: str = "limit",
    limit_price: str | None = "500.00",
    rationale: str = "test rationale",
) -> int:
    result = await db.execute(
        text(
            "INSERT INTO proposal "
            "(signal_id, ticker, side, quantity, order_type, limit_price, rationale, "
            "data_ts, model_ts) "
            "VALUES (:signal_id, :ticker, :side, :quantity, :order_type, :limit_price, "
            ":rationale, :data_ts, :model_ts) RETURNING id"
        ),
        {
            "signal_id": signal_id,
            "ticker": ticker,
            "side": side,
            "quantity": quantity,
            "order_type": order_type,
            "limit_price": limit_price,
            "rationale": rationale,
            "data_ts": datetime.now(timezone.utc),
            "model_ts": datetime.now(timezone.utc),
        },
    )
    return result.scalar_one()


async def _insert_approval(
    db,
    proposal_id: int,
    *,
    operator_id: str = "op",
    decision: str = "approved",
    reason: str | None = None,
) -> int:
    result = await db.execute(
        text(
            "INSERT INTO approval "
            "(proposal_id, operator_id, decision, reason, data_ts, model_ts) "
            "VALUES (:proposal_id, :operator_id, :decision, :reason, :data_ts, :model_ts) "
            "RETURNING id"
        ),
        {
            "proposal_id": proposal_id,
            "operator_id": operator_id,
            "decision": decision,
            "reason": reason,
            "data_ts": datetime.now(timezone.utc),
            "model_ts": datetime.now(timezone.utc),
        },
    )
    return result.scalar_one()


# ---------------------------------------------------------------------------
# _send_order tests
# ---------------------------------------------------------------------------

def test_paper_base_url_is_fixed():
    from app.modules.paper_execution_entry_point import PAPER_BASE_URL
    assert PAPER_BASE_URL == "https://paper-api.alpaca.markets"


@pytest.mark.asyncio
async def test_send_order_passes_order_type_to_client_parameter():
    client = AsyncMock()
    client.submit_order = AsyncMock(return_value=SimpleNamespace(id="o1", status="accepted"))
    proposal = {
        "id": 1,
        "ticker": "SPY",
        "symbol": "SPY",
        "side": "buy",
        "quantity": "1",
        "order_type": "limit",
        "limit_price": "500.00",
    }

    await _send_order(client, proposal)

    client.submit_order.assert_awaited_once()
    kwargs = client.submit_order.await_args.kwargs
    assert kwargs["order_type"] == "limit"
    assert kwargs["symbol"] == "SPY"
    assert kwargs["side"] == "buy"


@pytest.mark.asyncio
async def test_send_order_passes_all_optional_fields():
    client = AsyncMock()
    client.submit_order = AsyncMock(return_value=SimpleNamespace(id="o1", status="accepted"))
    proposal = {
        "id": 1,
        "ticker": "SPY",
        "symbol": "SPY",
        "side": "buy",
        "quantity": "1",
        "order_type": "limit",
        "limit_price": "500.00",
        "time_in_force": "day",
        "client_order_id": "cid-1",
    }

    await _send_order(client, proposal)

    kwargs = client.submit_order.await_args.kwargs
    assert kwargs["limit_price"] == "500.00"
    assert kwargs["time_in_force"] == "day"
    assert kwargs["client_order_id"] == "cid-1"
    assert kwargs["qty"] == "1"


@pytest.mark.asyncio
async def test_send_order_rejects_invalid_side():
    client = AsyncMock()
    proposal = {
        "ticker": "SPY",
        "side": "short",
        "quantity": "1",
        "order_type": "market",
    }

    with pytest.raises(ValueError):
        await _send_order(client, proposal)
    client.submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_send_order_rejects_invalid_order_type():
    client = AsyncMock()
    proposal = {
        "ticker": "SPY",
        "side": "buy",
        "quantity": "1",
        "order_type": "invalid",
    }

    with pytest.raises(ValueError):
        await _send_order(client, proposal)
    client.submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_send_order_rejects_invalid_time_in_force():
    client = AsyncMock()
    proposal = {
        "ticker": "SPY",
        "side": "buy",
        "quantity": "1",
        "order_type": "market",
        "time_in_force": "invalid",
    }

    with pytest.raises(ValueError):
        await _send_order(client, proposal)
    client.submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_send_order_rejects_non_positive_qty():
    client = AsyncMock()
    proposal = {
        "ticker": "SPY",
        "side": "buy",
        "quantity": "0",
        "order_type": "market",
    }

    with pytest.raises(ValueError):
        await _send_order(client, proposal)
    client.submit_order.assert_not_called()


@pytest.mark.asyncio
async def test_send_order_propagates_alpaca_error():
    client = AsyncMock()
    client.submit_order = AsyncMock(side_effect=RuntimeError("alpaca down"))
    proposal = {
        "ticker": "SPY",
        "side": "buy",
        "quantity": "1",
        "order_type": "market",
    }

    with pytest.raises(RuntimeError):
        await _send_order(client, proposal)


# ---------------------------------------------------------------------------
# run_execution_entry_point orchestration tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_execution_entry_point_sends_approved_orders_and_records_broker_id():
    async with async_session() as db:
        ingested_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, ingested_id)
        proposal_id = await _insert_proposal(db, signal_id)
        approval_id = await _insert_approval(db, proposal_id)
        await db.commit()

        fake_order = SimpleNamespace(id="broker-abc-123")
        fake_client = AsyncMock()
        fake_client.submit_order = AsyncMock(return_value=fake_order)
        fake_client.close = AsyncMock()

        with patch(
            "app.modules.paper_execution_entry_point.AlpacaPaperClient",
            return_value=fake_client,
        ):
            result = await run_execution_entry_point(
                db=db,
                operator_id="op",
                timestamp=datetime.now(timezone.utc),
            )

        rows = (
            await db.execute(
                text(
                    "SELECT broker_order_id FROM broker_order_id "
                    "WHERE approval_id = :aid"
                ),
                {"aid": approval_id},
            )
        ).fetchall()

        assert len(rows) == 1
        assert rows[0][0] == "broker-abc-123"
        assert result["orders"] == 1
        assert result["approvals"] == 1
        assert result["proposals"] == 1


@pytest.mark.asyncio
async def test_run_execution_entry_point_skips_already_executed_approvals():
    async with async_session() as db:
        ingested_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, ingested_id)
        proposal_id = await _insert_proposal(db, signal_id)
        approval_id = await _insert_approval(db, proposal_id)
        await db.commit()

        # Mark this approval as already executed.
        await db.execute(
            text(
                "INSERT INTO broker_order_id "
                "(approval_id, proposal_id, broker_order_id, data_ts, model_ts, submitted_ts) "
                "VALUES (:aid, :pid, :boi, :ts, :ts, :ts)"
            ),
            {
                "aid": approval_id,
                "pid": proposal_id,
                "boi": "already-sent",
                "ts": datetime.now(timezone.utc),
            },
        )
        await db.commit()

        # A second, still-pending approval.
        proposal_id2 = await _insert_proposal(db, signal_id, ticker="AAPL")
        approval_id2 = await _insert_approval(db, proposal_id2)
        await db.commit()

        fake_order = SimpleNamespace(id="broker-new-1")
        fake_client = AsyncMock()
        fake_client.submit_order = AsyncMock(return_value=fake_order)
        fake_client.close = AsyncMock()

        with patch(
            "app.modules.paper_execution_entry_point.AlpacaPaperClient",
            return_value=fake_client,
        ):
            result = await run_execution_entry_point(
                db=db,
                operator_id="op",
                timestamp=datetime.now(timezone.utc),
            )

        rows = (
            await db.execute(
                text(
                    "SELECT approval_id FROM broker_order_id "
                    "WHERE broker_order_id = 'broker-new-1'"
                )
            )
        ).fetchall()

        assert len(rows) == 1
        assert rows[0][0] == approval_id2
        assert result["orders"] == 1


@pytest.mark.asyncio
async def test_run_execution_entry_point_handles_send_failure_and_continues():
    async with async_session() as db:
        ingested_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, ingested_id)
        proposal_id1 = await _insert_proposal(db, signal_id, ticker="SPY")
        approval_id1 = await _insert_approval(db, proposal_id1)
        proposal_id2 = await _insert_proposal(db, signal_id, ticker="AAPL")
        approval_id2 = await _insert_approval(db, proposal_id2)
        await db.commit()

        fake_client = AsyncMock()
        fake_client.submit_order = AsyncMock(
            side_effect=[RuntimeError("alpaca down"), SimpleNamespace(id="broker-ok")]
        )
        fake_client.close = AsyncMock()

        with patch(
            "app.modules.paper_execution_entry_point.AlpacaPaperClient",
            return_value=fake_client,
        ):
            result = await run_execution_entry_point(
                db=db,
                operator_id="op",
                timestamp=datetime.now(timezone.utc),
            )

        rows = (
            await db.execute(
                text(
                    "SELECT approval_id FROM broker_order_id "
                    "WHERE broker_order_id = 'broker-ok'"
                )
            )
        ).fetchall()

        assert len(rows) == 1
        assert rows[0][0] == approval_id2
        assert result["orders"] == 1
        assert len(result["results"]) == 2
        statuses = {r["status"] for r in result["results"]}
        assert statuses == {"error", "sent"}
