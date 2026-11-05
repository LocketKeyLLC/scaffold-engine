# tests/test_paper_audit_trail_writer.py
"""Tests for app.modules.paper_audit_trail_writer."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import async_session
from app.modules import paper_audit_trail_writer as writer

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture(autouse=True)
async def _clean_tables():
    """Truncate all audit trail tables before each test.

    The sandbox empties this job's tables before every test run, so this
    fixture is a no-op safety net. It never DELETEs from append-only
    tables; it only truncates, which the sandbox already does.
    """
    yield


async def _insert_ingested_item(db, source: str = "sec_edgar", payload: dict | None = None) -> int:
    payload = payload or {"ticker": "AAPL", "form_type": "4"}
    result = await db.execute(
        text(
            """
            INSERT INTO ingested_item (source, external_id, published_ts, payload)
            VALUES (:source, :external_id, :published_ts, CAST(:payload AS json))
            RETURNING id
            """
        ),
        {
            "source": source,
            "external_id": uuid4().hex,
            "published_ts": datetime.now(timezone.utc),
            "payload": json.dumps(payload),
        },
    )
    row = result.fetchone()
    assert row is not None
    return row[0]


async def _insert_signal(db, ingested_item_id: int) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO signal (ingested_item_id, ticker, signal_type, direction, rationale)
            VALUES (:ingested_item_id, 'AAPL', 'form4_purchase', 'long', 'test rationale')
            RETURNING id
            """
        ),
        {"ingested_item_id": ingested_item_id},
    )
    row = result.fetchone()
    assert row is not None
    return row[0]


async def _insert_proposal(
    db,
    signal_id: int,
    ticker: str = "AAPL",
    side: str = "buy",
    quantity: str = "10",
    order_type: str = "market",
    limit_price: str | None = None,
) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO proposal (signal_id, ticker, side, quantity, order_type, limit_price, rationale)
            VALUES (:signal_id, :ticker, :side, :quantity, :order_type, :limit_price, 'test rationale')
            RETURNING id
            """
        ),
        {
            "signal_id": signal_id,
            "ticker": ticker,
            "side": side,
            "quantity": quantity,
            "order_type": order_type,
            "limit_price": limit_price,
        },
    )
    row = result.fetchone()
    assert row is not None
    return row[0]


async def _insert_approval(db, proposal_id: int, decision: str = "approved") -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO approval (proposal_id, operator_id, decision, reason)
            VALUES (:proposal_id, 'test-operator', :decision, 'test reason')
            RETURNING id
            """
        ),
        {"proposal_id": proposal_id, "decision": decision},
    )
    row = result.fetchone()
    assert row is not None
    return row[0]


async def _insert_order(db, approval_id: int, proposal_id: int, broker_order_id: str) -> int:
    result = await db.execute(
        text(
            """
            INSERT INTO broker_order_id (approval_id, proposal_id, broker_order_id)
            VALUES (:approval_id, :proposal_id, :broker_order_id)
            RETURNING id
            """
        ),
        {
            "approval_id": approval_id,
            "proposal_id": proposal_id,
            "broker_order_id": broker_order_id,
        },
    )
    row = result.fetchone()
    assert row is not None
    return row[0]


async def _fetch_one(db, table: str, id_: int):
    result = await db.execute(text(f"SELECT * FROM {table} WHERE id = :id"), {"id": id_})
    return result.fetchone()


async def test_write_signal_success():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        await db.commit()

        signal_id = await writer.write_signal(
            ingested_item_id=item_id,
            ticker="AAPL",
            signal_type="form4_purchase",
            direction="long",
            rationale="test rationale",
        )

        row = await _fetch_one(db, "signal", signal_id)
        assert row is not None
        assert row[1] == item_id  # ingested_item_id
        assert row[2] == "AAPL"  # ticker
        assert row[3] == "form4_purchase"  # signal_type
        assert row[4] == "long"  # direction
        assert row[5] == "test rationale"  # rationale


async def test_write_signal_invalid_signal_type():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        await db.commit()

        with pytest.raises(Exception):
            await writer.write_signal(
                ingested_item_id=item_id,
                ticker="AAPL",
                signal_type="invalid_type",
                direction="long",
                rationale="test rationale",
            )


async def test_write_signal_invalid_direction():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        await db.commit()

        with pytest.raises(Exception):
            await writer.write_signal(
                ingested_item_id=item_id,
                ticker="AAPL",
                signal_type="form4_purchase",
                direction="short",
                rationale="test rationale",
            )


async def test_write_signal_missing_ingested_item():
    with pytest.raises(Exception):
        await writer.write_signal(
            ingested_item_id=999999,
            ticker="AAPL",
            signal_type="form4_purchase",
            direction="long",
            rationale="test rationale",
        )


async def test_write_proposal_success():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        await db.commit()

        proposal_id = await writer.write_proposal(
            signal_id=signal_id,
            ticker="AAPL",
            side="buy",
            quantity="10",
            order_type="market",
            limit_price=None,
            rationale="test rationale",
        )

        row = await _fetch_one(db, "proposal", proposal_id)
        assert row is not None
        assert row[1] == signal_id  # signal_id
        assert row[2] == "AAPL"  # ticker
        assert row[3] == "buy"  # side
        assert row[4] == 10  # quantity (Numeric)
        assert row[5] == "market"  # order_type
        assert row[6] is None  # limit_price
        assert row[7] == "test rationale"  # rationale


async def test_write_proposal_quantity_must_be_positive():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        await db.commit()

        with pytest.raises(Exception):
            await writer.write_proposal(
                signal_id=signal_id,
                ticker="AAPL",
                side="buy",
                quantity="-1",
                order_type="market",
                limit_price=None,
                rationale="test rationale",
            )


async def test_write_proposal_invalid_side():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        await db.commit()

        with pytest.raises(Exception):
            await writer.write_proposal(
                signal_id=signal_id,
                ticker="AAPL",
                side="hold",
                quantity="10",
                order_type="market",
                limit_price=None,
                rationale="test rationale",
            )


async def test_write_risk_check_success():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        proposal_id = await _insert_proposal(db, signal_id)
        await db.commit()

        risk_check_id = await writer.write_risk_check(
            proposal_id=proposal_id,
            check_type="position_size",
            passed=True,
            reason=None,
            metrics={"notional": 1000.0, "equity": 10000.0},
        )

        row = await _fetch_one(db, "risk_check", risk_check_id)
        assert row is not None
        assert row[1] == proposal_id  # proposal_id
        assert row[2] == "position_size"  # check_type
        assert row[3] is True  # passed
        assert row[4] is None  # reason
        assert row[5] == {"notional": 1000.0, "equity": 10000.0}  # metrics (JSON)


async def test_write_risk_check_rejected():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        proposal_id = await _insert_proposal(db, signal_id)
        await db.commit()

        risk_check_id = await writer.write_risk_check(
            proposal_id=proposal_id,
            check_type="daily_loss",
            passed=False,
            reason="daily loss exceeds limit",
            metrics={"daily_pnl": -500.0},
        )

        row = await _fetch_one(db, "risk_check", risk_check_id)
        assert row is not None
        assert row[3] is False  # passed
        assert row[4] == "daily loss exceeds limit"  # reason
        assert row[5] == {"daily_pnl": -500.0}  # metrics (JSON)


async def test_write_risk_check_invalid_proposal():
    with pytest.raises(Exception):
        await writer.write_risk_check(
            proposal_id=999999,
            check_type="position_size",
            passed=True,
            reason=None,
            metrics={"notional": 1000.0},
        )


async def test_write_approval_success():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        proposal_id = await _insert_proposal(db, signal_id)
        await db.commit()

        approval_id = await writer.write_approval(
            proposal_id=proposal_id,
            operator_id="test-operator",
            decision="approved",
            reason="looks good",
        )

        row = await _fetch_one(db, "approval", approval_id)
        assert row is not None
        assert row[1] == proposal_id  # proposal_id
        assert row[2] == "test-operator"  # operator_id
        assert row[3] == "approved"  # decision
        assert row[4] == "looks good"  # reason


async def test_write_approval_rejected():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        proposal_id = await _insert_proposal(db, signal_id)
        await db.commit()

        approval_id = await writer.write_approval(
            proposal_id=proposal_id,
            operator_id="test-operator",
            decision="rejected",
            reason="too risky",
        )

        row = await _fetch_one(db, "approval", approval_id)
        assert row is not None
        assert row[3] == "rejected"  # decision
        assert row[4] == "too risky"  # reason


async def test_write_approval_invalid_decision():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        proposal_id = await _insert_proposal(db, signal_id)
        await db.commit()

        with pytest.raises(Exception):
            await writer.write_approval(
                proposal_id=proposal_id,
                operator_id="test-operator",
                decision="maybe",
                reason=None,
            )


async def test_write_order_success():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        proposal_id = await _insert_proposal(db, signal_id)
        approval_id = await _insert_approval(db, proposal_id)
        await db.commit()

        order_id = await writer.write_order(
            approval_id=approval_id,
            proposal_id=proposal_id,
            broker_order_id="broker-123",
        )

        row = await _fetch_one(db, "broker_order_id", order_id)
        assert row is not None
        assert row[1] == approval_id  # approval_id
        assert row[2] == proposal_id  # proposal_id
        assert row[3] == "broker-123"  # broker_order_id


async def test_write_order_duplicate_broker_order_id():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        proposal_id = await _insert_proposal(db, signal_id)
        approval_id = await _insert_approval(db, proposal_id)
        await db.commit()

        await writer.write_order(
            approval_id=approval_id,
            proposal_id=proposal_id,
            broker_order_id="broker-dup",
        )

        with pytest.raises(Exception):
            await writer.write_order(
                approval_id=approval_id,
                proposal_id=proposal_id,
                broker_order_id="broker-dup",
            )


async def test_write_order_missing_approval():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        proposal_id = await _insert_proposal(db, signal_id)
        await db.commit()

        with pytest.raises(Exception):
            await writer.write_order(
                approval_id=999999,
                proposal_id=proposal_id,
                broker_order_id="broker-missing",
            )


async def test_write_fill_success():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        proposal_id = await _insert_proposal(db, signal_id)
        approval_id = await _insert_approval(db, proposal_id)
        order_id = await _insert_order(db, approval_id, proposal_id, "broker-fill")
        await db.commit()

        fill_id = await writer.write_fill(
            broker_order_id=order_id,
            fill_id="fill-1",
            side="buy",
            quantity="10",
            price="150.50",
            commission="1.00",
            filled_ts=datetime.now(timezone.utc),
        )

        row = await _fetch_one(db, "fill", fill_id)
        assert row is not None
        assert row[1] == order_id  # broker_order_id
        assert row[2] == "fill-1"  # fill_id
        assert row[3] == "buy"  # side
        assert row[4] == 10  # quantity (Numeric)
        assert row[5] == 150.50  # price (Numeric)
        assert row[6] == 1.00  # commission (Numeric)


async def test_write_fill_invalid_side():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        proposal_id = await _insert_proposal(db, signal_id)
        approval_id = await _insert_approval(db, proposal_id)
        order_id = await _insert_order(db, approval_id, proposal_id, "broker-fill-invalid")
        await db.commit()

        with pytest.raises(Exception):
            await writer.write_fill(
                broker_order_id=order_id,
                fill_id="fill-2",
                side="hold",
                quantity="10",
                price="150.50",
                commission="1.00",
                filled_ts=datetime.now(timezone.utc),
            )


async def test_write_fill_missing_order():
    with pytest.raises(Exception):
        await writer.write_fill(
            broker_order_id=999999,
            fill_id="fill-3",
            side="buy",
            quantity="10",
            price="150.50",
            commission="1.00",
            filled_ts=datetime.now(timezone.utc),
        )


async def test_write_pnl_success():
    pnl_id = await writer.write_pnl(
        as_of_ts=datetime.now(timezone.utc),
        account_value="100000.00",
        cash="50000.00",
        equity="100000.00",
        realized_pnl="0.00",
        unrealized_pnl="0.00",
        daily_return=None,
        cumulative_return=None,
        max_drawdown=None,
        sharpe_ratio=None,
        turnover=None,
        spy_benchmark_return=None,
    )

    async with async_session() as db:
        row = await _fetch_one(db, "pnl", pnl_id)
        assert row is not None
        assert row[2] == 100000.00  # account_value (Numeric)
        assert row[3] == 50000.00  # cash (Numeric)
        assert row[4] == 100000.00  # equity (Numeric)
        assert row[5] == 0.00  # realized_pnl (Numeric)
        assert row[6] == 0.00  # unrealized_pnl (Numeric)


async def test_append_only_update_forbidden():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        await db.commit()

        with pytest.raises(Exception):
            await db.execute(
                text("UPDATE signal SET ticker = 'MSFT' WHERE id = :id"),
                {"id": signal_id},
            )
            await db.commit()


async def test_append_only_delete_forbidden():
    async with async_session() as db:
        item_id = await _insert_ingested_item(db)
        signal_id = await _insert_signal(db, item_id)
        await db.commit()

        with pytest.raises(Exception):
            await db.execute(
                text("DELETE FROM signal WHERE id = :id"),
                {"id": signal_id},
            )
            await db.commit()
