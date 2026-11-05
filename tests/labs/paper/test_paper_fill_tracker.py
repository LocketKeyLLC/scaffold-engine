# tests/test_paper_fill_tracker.py
"""Tests for the Alpaca paper fill tracker websocket module."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_fill_tracker import (
    FillTrackerAuthError,
    FillTrackerError,
    ParsedFill,
    _fill_exists,
    _flush_buffered,
    _write_fill,
    is_trade_update,
    parse_envelope,
    parse_fill_event,
    track_fills,
)


def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _fill_envelope(
    *,
    event: str = "fill",
    execution_id: str = "exec-1",
    order_id: str = "order-1",
    side: str = "buy",
    symbol: str = "AAPL",
    qty: str = "0.5",
    price: str = "150.25",
    timestamp: str = "2026-10-23T20:30:00Z",
    filled_at: str = "2026-10-23T20:30:00Z",
) -> dict:
    return {
        "stream": "trade_updates",
        "data": {
            "event": event,
            "execution_id": execution_id,
            "order": {
                "id": order_id,
                "side": side,
                "symbol": symbol,
                "filled_at": filled_at,
            },
            "qty": qty,
            "price": price,
            "timestamp": timestamp,
        },
    }


class _FakeDbSession:
    """Async context manager stand-in for auth tests that need no SQL."""

    def __init__(self):
        self.secrets = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        return False

    async def execute(self, *args, **kwargs):
        result = MagicMock()
        result.scalar.return_value = None
        result.first.return_value = None
        return result

    async def commit(self):
        pass


def _fake_async_session():
    return _FakeDbSession()


async def _insert_broker_order_id_chain(
    db, *, broker_order_id: str, ticker: str = "AAPL"
) -> int:
    """Insert the full parent chain and return broker_order_id.id."""
    ext_id = uuid4().hex
    result = await db.execute(
        text(
            "INSERT INTO ingested_item (source, external_id, published_ts, payload) "
            "VALUES (:source, :external_id, :published_ts, CAST(:payload AS json)) "
            "RETURNING id"
        ),
        {
            "source": "news",
            "external_id": ext_id,
            "published_ts": _ts("2026-10-23T20:00:00Z"),
            "payload": json.dumps({}),
        },
    )
    ingested_id = result.scalar_one()
    result = await db.execute(
        text(
            "INSERT INTO signal (ingested_item_id, ticker, signal_type, direction, rationale) "
            "VALUES (:ingested_item_id, :ticker, :signal_type, :direction, :rationale) "
            "RETURNING id"
        ),
        {
            "ingested_item_id": ingested_id,
            "ticker": ticker,
            "signal_type": "news_context",
            "direction": "long",
            "rationale": "test",
        },
    )
    signal_id = result.scalar_one()
    result = await db.execute(
        text(
            "INSERT INTO proposal (signal_id, ticker, side, quantity, order_type, rationale) "
            "VALUES (:signal_id, :ticker, :side, :quantity, :order_type, :rationale) "
            "RETURNING id"
        ),
        {
            "signal_id": signal_id,
            "ticker": ticker,
            "side": "buy",
            "quantity": "0.5",
            "order_type": "market",
            "rationale": "test",
        },
    )
    proposal_id = result.scalar_one()
    result = await db.execute(
        text(
            "INSERT INTO approval (proposal_id, operator_id, decision) "
            "VALUES (:proposal_id, :operator_id, :decision) RETURNING id"
        ),
        {
            "proposal_id": proposal_id,
            "operator_id": "operator",
            "decision": "approved",
        },
    )
    approval_id = result.scalar_one()
    result = await db.execute(
        text(
            "INSERT INTO broker_order_id (approval_id, proposal_id, broker_order_id) "
            "VALUES (:approval_id, :proposal_id, :broker_order_id) RETURNING id"
        ),
        {
            "approval_id": approval_id,
            "proposal_id": proposal_id,
            "broker_order_id": broker_order_id,
        },
    )
    return result.scalar_one()


async def _insert_fill(
    db,
    *,
    fill_id: str,
    broker_order_id: int,
    side: str = "buy",
    quantity: str = "0.5",
    price: str = "150.25",
    filled_ts: datetime | None = None,
) -> int:
    """Insert a fill row and return its id."""
    if filled_ts is None:
        filled_ts = _ts("2026-10-23T20:30:00Z")
    result = await db.execute(
        text(
            "INSERT INTO fill (broker_order_id, fill_id, side, quantity, price, filled_ts) "
            "VALUES (:broker_order_id, :fill_id, :side, :quantity, :price, :filled_ts) "
            "RETURNING id"
        ),
        {
            "broker_order_id": broker_order_id,
            "fill_id": fill_id,
            "side": side,
            "quantity": quantity,
            "price": price,
            "filled_ts": filled_ts,
        },
    )
    return result.scalar_one()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_track_fills_auth_missing_secrets():
    """Raises FillTrackerAuthError when secrets are not configured."""
    with patch(
        "app.modules.paper_fill_tracker.values_for",
        new=AsyncMock(return_value={}),
    ), patch(
        "app.modules.paper_fill_tracker.async_session",
        new=_fake_async_session,
    ):
        with pytest.raises(FillTrackerAuthError):
            await track_fills()


@pytest.mark.asyncio
async def test_track_fills_auth_success_and_subscribe():
    """Authenticates and subscribes to trade_updates using the documented listen message."""
    secrets = {
        "ALPACA_API_KEY_ID": "key-id",
        "ALPACA_API_SECRET_KEY": "secret-key",
    }
    auth_env = {
        "stream": "authorization",
        "data": {"status": "authorized"},
    }
    sub_env = {"stream": "listening", "data": {"streams": ["trade_updates"]}}

    mock_ws = MagicMock()
    mock_ws.__aenter__.return_value = mock_ws
    mock_ws.__aexit__ = AsyncMock(return_value=False)
    mock_ws.recv = AsyncMock(side_effect=[json.dumps(auth_env), json.dumps(sub_env)])
    mock_ws.send = AsyncMock()
    mock_ws.__aiter__.return_value = iter([])

    with patch(
        "app.modules.paper_fill_tracker.values_for",
        new=AsyncMock(return_value=secrets),
    ), patch(
        "app.modules.paper_fill_tracker.websockets.connect",
        return_value=mock_ws,
    ):
        await track_fills()

    assert mock_ws.send.call_count == 2
    auth_payload = json.loads(mock_ws.send.call_args_list[0].args[0])
    assert auth_payload["action"] == "auth"
    assert auth_payload["key"] == "key-id"
    assert auth_payload["secret"] == "secret-key"
    sub_payload = json.loads(mock_ws.send.call_args_list[1].args[0])
    assert sub_payload["action"] == "listen"
    assert sub_payload["data"]["streams"] == ["trade_updates"]


@pytest.mark.asyncio
async def test_track_fills_auth_rejected_raises():
    """Raises FillTrackerAuthError when Alpaca rejects the auth."""
    secrets = {
        "ALPACA_API_KEY_ID": "key-id",
        "ALPACA_API_SECRET_KEY": "secret-key",
    }
    auth_env = {
        "stream": "authorization",
        "data": {"status": "unauthorized"},
    }

    mock_ws = MagicMock()
    mock_ws.__aenter__.return_value = mock_ws
    mock_ws.__aexit__ = AsyncMock(return_value=False)
    mock_ws.recv = AsyncMock(return_value=json.dumps(auth_env))
    mock_ws.send = AsyncMock()

    with patch(
        "app.modules.paper_fill_tracker.values_for",
        new=AsyncMock(return_value=secrets),
    ), patch(
        "app.modules.paper_fill_tracker.async_session",
        new=_fake_async_session,
    ), patch(
        "app.modules.paper_fill_tracker.websockets.connect",
        return_value=mock_ws,
    ):
        with pytest.raises(FillTrackerAuthError):
            await track_fills()


# ---------------------------------------------------------------------------
# Envelope parsing
# ---------------------------------------------------------------------------


def test_parse_envelope_bytes():
    raw = json.dumps({"stream": "trade_updates"}).encode("utf-8")
    assert parse_envelope(raw) == {"stream": "trade_updates"}


def test_is_trade_update():
    assert is_trade_update({"stream": "trade_updates"}) is True
    assert is_trade_update({"stream": "other"}) is False


def test_parse_fill_event_full():
    env = _fill_envelope()
    fill = parse_fill_event(env)
    assert fill is not None
    assert fill.execution_id == "exec-1"
    assert fill.order_id == "order-1"
    assert fill.side == "buy"
    assert fill.symbol == "AAPL"
    assert fill.qty == Decimal("0.5")
    assert fill.price == Decimal("150.25")
    assert fill.timestamp == _ts("2026-10-23T20:30:00Z")
    assert fill.filled_at == _ts("2026-10-23T20:30:00Z")


def test_parse_fill_event_partial_fill():
    env = _fill_envelope(event="partial_fill", qty="0.25")
    fill = parse_fill_event(env)
    assert fill is not None
    assert fill.qty == Decimal("0.25")


def test_parse_fill_event_non_fill_returns_none():
    env = _fill_envelope(event="new")
    assert parse_fill_event(env) is None


def test_parse_fill_event_missing_execution_id_raises():
    env = _fill_envelope()
    del env["data"]["execution_id"]
    with pytest.raises(ValueError):
        parse_fill_event(env)


def test_parse_fill_event_missing_order_raises():
    env = _fill_envelope()
    del env["data"]["order"]
    with pytest.raises(ValueError):
        parse_fill_event(env)


def test_parse_fill_event_missing_qty_raises():
    env = _fill_envelope()
    del env["data"]["qty"]
    with pytest.raises(ValueError):
        parse_fill_event(env)


# ---------------------------------------------------------------------------
# Per-fill economics
# ---------------------------------------------------------------------------


def test_parse_fill_event_decimal_precision():
    env = _fill_envelope(qty="0.333333", price="199.999999")
    fill = parse_fill_event(env)
    assert fill.qty == Decimal("0.333333")
    assert fill.price == Decimal("199.999999")


# ---------------------------------------------------------------------------
# Commit behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_buffered_commits_after_each_insert():
    """Each successful write_fill is followed by db.commit()."""
    fill = ParsedFill(
        execution_id="exec-1",
        order_id="order-1",
        side="buy",
        symbol="AAPL",
        qty=Decimal("0.5"),
        price=Decimal("150.25"),
        timestamp=_ts("2026-10-23T20:30:00Z"),
        filled_at=_ts("2026-10-23T20:30:00Z"),
    )

    async with async_session() as db:
        await _insert_broker_order_id_chain(db, broker_order_id="order-1")
        await db.commit()

    async with async_session() as db:
        remaining, seen, last_ts = await _flush_buffered(
            db, [fill], set(), None
        )

    assert remaining == []
    assert seen == {"exec-1"}
    assert last_ts == _ts("2026-10-23T20:30:00Z")

    async with async_session() as db:
        result = await db.execute(
            text("SELECT quantity FROM fill WHERE fill_id = :fill_id"),
            {"fill_id": "exec-1"},
        )
        row = result.first()
        assert row is not None
        assert Decimal(str(row.quantity)) == Decimal("0.5")


# ---------------------------------------------------------------------------
# Dedupe
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_buffered_dedupes_by_execution_id():
    """A fill already in the seen set is skipped."""
    fill = ParsedFill(
        execution_id="exec-1",
        order_id="order-1",
        side="buy",
        symbol="AAPL",
        qty=Decimal("0.5"),
        price=Decimal("150.25"),
        timestamp=_ts("2026-10-23T20:30:00Z"),
        filled_at=_ts("2026-10-23T20:30:00Z"),
    )

    async with async_session() as db:
        remaining, seen, last_ts = await _flush_buffered(
            db, [fill], {"exec-1"}, None
        )

    assert remaining == []
    assert seen == {"exec-1"}
    assert last_ts is None




# ---------------------------------------------------------------------------
# Out-of-order reconciliation
# ---------------------------------------------------------------------------




@pytest.mark.asyncio
async def test_flush_buffered_drops_late_fill_after_gap():
    """A fill with filled_ts <= last_flushed_ts is dropped."""
    fill_late = ParsedFill(
        execution_id="exec-late",
        order_id="order-late",
        side="buy",
        symbol="AAPL",
        qty=Decimal("0.5"),
        price=Decimal("150.25"),
        timestamp=_ts("2026-10-23T20:29:00Z"),
        filled_at=_ts("2026-10-23T20:29:00Z"),
    )

    async with async_session() as db:
        remaining, seen, last_ts = await _flush_buffered(
            db, [fill_late], set(), _ts("2026-10-23T20:30:00Z")
        )

    assert remaining == []
    assert seen == set()
    assert last_ts == _ts("2026-10-23T20:30:00Z")


# ---------------------------------------------------------------------------
# Partial fills
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_fill_written_with_fractional_qty():
    """A partial_fill event is parsed and written with its qty."""
    env = _fill_envelope(
        event="partial_fill",
        execution_id="exec-partial-1",
        qty="0.25",
        price="150.25",
    )
    fill = parse_fill_event(env)
    assert fill is not None
    assert fill.qty == Decimal("0.25")

    async with async_session() as db:
        await _insert_broker_order_id_chain(db, broker_order_id="order-1")
        await db.commit()

    async with async_session() as db:
        remaining, seen, last_ts = await _flush_buffered(
            db, [fill], set(), None
        )

    assert remaining == []
    assert seen == {"exec-partial-1"}

    async with async_session() as db:
        result = await db.execute(
            text("SELECT quantity, side FROM fill WHERE fill_id = :fill_id"),
            {"fill_id": "exec-partial-1"},
        )
        row = result.first()
        assert row is not None
        assert Decimal(str(row.quantity)) == Decimal("0.25")
        assert row.side == "buy"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flush_buffered_keeps_fill_on_write_error():
    """A fill that fails to write stays in the buffer."""
    fill = ParsedFill(
        execution_id="exec-1",
        order_id="nonexistent-order",
        side="buy",
        symbol="AAPL",
        qty=Decimal("0.5"),
        price=Decimal("150.25"),
        timestamp=_ts("2026-10-23T20:30:00Z"),
        filled_at=_ts("2026-10-23T20:30:00Z"),
    )

    async with async_session() as db:
        remaining, seen, last_ts = await _flush_buffered(db, [fill], set(), None)

    assert remaining == [fill]
    assert seen == set()
    assert last_ts is None


@pytest.mark.asyncio
async def test_track_fills_malformed_message_skipped():
    """A malformed websocket message is logged and skipped."""
    secrets = {
        "ALPACA_API_KEY_ID": "key-id",
        "ALPACA_API_SECRET_KEY": "secret-key",
    }
    auth_env = {
        "stream": "authorization",
        "data": {"status": "authorized"},
    }
    sub_env = {"stream": "listening", "data": {"streams": ["trade_updates"]}}

    mock_ws = MagicMock()
    mock_ws.__aenter__.return_value = mock_ws
    mock_ws.__aexit__ = AsyncMock(return_value=False)
    mock_ws.recv = AsyncMock(side_effect=[json.dumps(auth_env), json.dumps(sub_env)])
    mock_ws.send = AsyncMock()
    mock_ws.__aiter__.return_value = iter(["not-json{{{"])

    with patch(
        "app.modules.paper_fill_tracker.values_for",
        new=AsyncMock(return_value=secrets),
    ), patch(
        "app.modules.paper_fill_tracker.websockets.connect",
        return_value=mock_ws,
    ):
        await track_fills()

    # No exception raised; the malformed message was skipped.


@pytest.mark.asyncio
async def test_flush_buffered_dedupes_existing_db_fill():
    """A fill already present in the fill table is skipped."""
    fill = ParsedFill(
        execution_id="exec-existing",
        order_id="order-existing",
        side="buy",
        symbol="AAPL",
        qty=Decimal("0.5"),
        price=Decimal("150.25"),
        timestamp=_ts("2026-10-23T20:30:00Z"),
        filled_at=_ts("2026-10-23T20:30:00Z"),
    )

    async with async_session() as db:
        broker_id = await _insert_broker_order_id_chain(
            db, broker_order_id="order-existing"
        )
        await _insert_fill(db, fill_id="exec-existing", broker_order_id=broker_id)
        await db.commit()

    async with async_session() as db:
        remaining, seen, last_ts = await _flush_buffered(
            db, [fill], set(), None
        )

    assert remaining == []
    assert seen == {"exec-existing"}
    assert last_ts is None

    # Verify no duplicate fill was inserted.
    async with async_session() as db:
        result = await db.execute(
            text("SELECT COUNT(*) FROM fill WHERE fill_id = :fill_id"),
            {"fill_id": "exec-existing"},
        )
        count = result.scalar_one()
        assert count == 1


@pytest.mark.asyncio
async def test_flush_buffered_sorts_out_of_order():
    """Fills are written in filled_ts order regardless of arrival order."""
    fill_late = ParsedFill(
        execution_id="exec-late",
        order_id="order-late",
        side="buy",
        symbol="AAPL",
        qty=Decimal("0.5"),
        price=Decimal("150.25"),
        timestamp=_ts("2026-10-23T20:31:00Z"),
        filled_at=_ts("2026-10-23T20:31:00Z"),
    )
    fill_early = ParsedFill(
        execution_id="exec-early",
        order_id="order-early",
        side="buy",
        symbol="AAPL",
        qty=Decimal("0.5"),
        price=Decimal("150.25"),
        timestamp=_ts("2026-10-23T20:30:00Z"),
        filled_at=_ts("2026-10-23T20:30:00Z"),
    )

    async with async_session() as db:
        await _insert_broker_order_id_chain(db, broker_order_id="order-late")
        await _insert_broker_order_id_chain(db, broker_order_id="order-early")
        await db.commit()

    async with async_session() as db:
        remaining, seen, last_ts = await _flush_buffered(
            db, [fill_late, fill_early], set(), None
        )

    assert remaining == []
    assert seen == {"exec-late", "exec-early"}
    assert last_ts == _ts("2026-10-23T20:31:00Z")

    async with async_session() as db:
        result = await db.execute(
            text(
                "SELECT fill_id, filled_ts FROM fill "
                "WHERE fill_id IN ('exec-early', 'exec-late') "
                "ORDER BY filled_ts"
            )
        )
        rows = result.fetchall()
        assert len(rows) == 2
        assert rows[0].fill_id == "exec-early"
        assert rows[1].fill_id == "exec-late"
