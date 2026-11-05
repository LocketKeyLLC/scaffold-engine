# app/modules/paper_fill_tracker.py
"""Websocket streaming of fractional-share paper order fills.

Tracks `trade_updates` events from the Alpaca paper stream, parses
`fill` and `partial_fill` envelopes, reconciles out-of-order fills by
`filled_ts`, and writes each fill to the audit trail via
`app.modules.paper_audit_trail_writer.write_fill`.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

import websockets
from sqlalchemy import text

from app.database import async_session
from app.modules.runner_secrets import values_for
from app.modules.paper_audit_trail_writer import write_fill

logger = logging.getLogger("scaffold")

PAPER_STREAM_URL: str = "wss://paper-api.alpaca.markets/stream"
ALPACA_API_KEY_ID_SECRET_NAME: str = "ALPACA_API_KEY_ID"
ALPACA_API_SECRET_KEY_SECRET_NAME: str = "ALPACA_API_SECRET_KEY"
TRADE_UPDATES_STREAM: str = "trade_updates"

MAX_BUFFERED_FILLS: int = 100


@dataclass(frozen=True)
class ParsedFill:
    """A single fill event, normalized for audit-trail writing."""

    execution_id: str
    order_id: str
    side: str
    symbol: str
    qty: Decimal
    price: Decimal
    timestamp: datetime
    filled_at: datetime | None


class FillTrackerError(Exception):
    """Raised when the fill tracker cannot continue."""


class FillTrackerAuthError(FillTrackerError):
    """Raised when Alpaca stream credentials are missing or rejected."""


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        logger.warning("Unparseable datetime: %r", value)
        return None


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, TypeError):
        logger.warning("Unparseable decimal: %r", value)
        return None


def parse_envelope(raw: str | bytes) -> dict[str, Any]:
    """Parse a raw websocket message into a dict envelope.

    Raises `json.JSONDecodeError` for malformed messages.
    """
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    return json.loads(raw)


def is_trade_update(envelope: dict[str, Any]) -> bool:
    """Return True if the envelope is a trade_updates stream message."""
    return envelope.get("stream") == TRADE_UPDATES_STREAM


def parse_fill_event(envelope: dict[str, Any]) -> ParsedFill | None:
    """Parse a trade_updates envelope into a ParsedFill.

    Returns None if the event is not a fill or partial_fill.
    Raises ValueError if a fill event is missing required fields.
    """
    data = envelope.get("data")
    if not isinstance(data, dict):
        return None

    event = data.get("event")
    if event not in ("fill", "partial_fill"):
        return None

    execution_id = data.get("execution_id")
    if not execution_id:
        raise ValueError("fill event missing execution_id")

    order = data.get("order")
    if not isinstance(order, dict):
        raise ValueError("fill event missing order dict")

    qty = _parse_decimal(data.get("qty"))
    if qty is None:
        raise ValueError("fill event missing or invalid qty")

    price = _parse_decimal(data.get("price"))
    if price is None:
        raise ValueError("fill event missing or invalid price")

    timestamp = _parse_datetime(data.get("timestamp"))
    if timestamp is None:
        raise ValueError("fill event missing or invalid timestamp")

    side = order.get("side")
    if not side:
        raise ValueError("fill event order missing side")

    symbol = order.get("symbol")
    if not symbol:
        raise ValueError("fill event order missing symbol")

    filled_at = _parse_datetime(order.get("filled_at"))

    return ParsedFill(
        execution_id=execution_id,
        order_id=order.get("id", ""),
        side=side,
        symbol=symbol,
        qty=qty,
        price=price,
        timestamp=timestamp,
        filled_at=filled_at,
    )


async def _fill_exists(db: object, execution_id: str) -> bool:
    """Return True if a fill with this execution_id already exists."""
    result = await db.execute(
        text("SELECT 1 FROM fill WHERE fill_id = :execution_id"),
        {"execution_id": execution_id},
    )
    return result.scalar() is not None


async def _broker_order_id_for_execution(
    db: object, order_id: str
) -> int | None:
    """Return the broker_order_id.id for a given Alpaca order id, or None."""
    result = await db.execute(
        text("SELECT id FROM broker_order_id WHERE broker_order_id = :order_id"),
        {"order_id": order_id},
    )
    row = result.first()
    return row.id if row else None


async def _write_fill(db: object, fill: ParsedFill) -> int:
    """Persist a single fill to the audit trail.

    Resolves the broker_order_id.id via the Alpaca order id, then calls
    the upstream `write_fill`. Returns the new fill row id.
    """
    broker_order_id = await _broker_order_id_for_execution(db, fill.order_id)
    if broker_order_id is None:
        raise FillTrackerError(
            f"No broker_order_id row for Alpaca order {fill.order_id}"
        )

    fill_id = await write_fill(
        broker_order_id=broker_order_id,
        fill_id=fill.execution_id,
        side=fill.side,
        quantity=str(fill.qty),
        price=str(fill.price),
        commission="0",
        filled_ts=fill.filled_at or fill.timestamp,
    )
    return fill_id


async def _flush_buffered(
    db: object,
    buffered: list[ParsedFill],
    seen: set[str],
    last_flushed_ts: datetime | None,
) -> tuple[list[ParsedFill], set[str], datetime | None]:
    """Flush buffered fills in `filled_ts` order, resolving gaps.

    Returns the remaining buffer, the updated seen set, and the new
    last-flushed timestamp. Fills with a `filled_ts` earlier than
    `last_flushed_ts` are dropped as duplicates/out-of-range.
    """
    if not buffered:
        return buffered, seen, last_flushed_ts

    def sort_key(f: ParsedFill) -> datetime:
        return f.filled_at or f.timestamp

    buffered.sort(key=sort_key)

    remaining: list[ParsedFill] = []
    for fill in buffered:
        if fill.execution_id in seen:
            continue
        eff_ts = fill.filled_at or fill.timestamp
        if last_flushed_ts is not None and eff_ts <= last_flushed_ts:
            logger.warning(
                "Dropping late fill %s with ts %s (last flushed %s)",
                fill.execution_id,
                eff_ts.isoformat(),
                last_flushed_ts.isoformat(),
            )
            continue
        if await _fill_exists(db, fill.execution_id):
            seen.add(fill.execution_id)
            continue
        try:
            await _write_fill(db, fill)
            await db.commit()
            seen.add(fill.execution_id)
            last_flushed_ts = eff_ts
        except FillTrackerError as exc:
            logger.error("Failed to write fill %s: %s", fill.execution_id, exc)
            remaining.append(fill)
    return remaining, seen, last_flushed_ts


async def track_fills() -> None:
    """Connect to the Alpaca paper stream and track trade_updates fills.

    Authenticates using secrets stored by the operator, subscribes to
    `trade_updates`, parses fill and partial_fill events, dedupes by
    execution_id, buffers out-of-order fills, and writes each fill to
    the audit trail with a commit after every insert.

    Runs until the websocket closes or an unrecoverable error occurs.
    """
    async with async_session() as db:
        secrets = await values_for(
            db,
            [ALPACA_API_KEY_ID_SECRET_NAME, ALPACA_API_SECRET_KEY_SECRET_NAME],
        )
        api_key_id = secrets.get(ALPACA_API_KEY_ID_SECRET_NAME)
        secret_key = secrets.get(ALPACA_API_SECRET_KEY_SECRET_NAME)
        if not api_key_id or not secret_key:
            raise FillTrackerAuthError(
                "Alpaca stream secrets not configured; store "
                "ALPACA_API_KEY_ID and ALPACA_API_SECRET_KEY"
            )

    auth_payload = json.dumps(
        {
            "action": "auth",
            "key": api_key_id,
            "secret": secret_key,
        }
    )
    listen_payload = json.dumps(
        {"action": "listen", "data": {"streams": [TRADE_UPDATES_STREAM]}}
    )

    buffered: list[ParsedFill] = []
    seen: set[str] = set()
    last_flushed_ts: datetime | None = None

    try:
        async with websockets.connect(PAPER_STREAM_URL) as ws:
            await ws.send(auth_payload)
            auth_response = await ws.recv()
            auth_env = parse_envelope(auth_response)
            if auth_env.get("stream") != "authorization":
                raise FillTrackerAuthError(
                    f"Unexpected auth response: {auth_response[:200]}"
                )
            if auth_env.get("data", {}).get("status") != "authorized":
                raise FillTrackerAuthError(
                    f"Alpaca stream auth failed: {auth_response[:200]}"
                )

            await ws.send(listen_payload)
            # Alpaca sends a listening confirmation; consume it.
            try:
                listen_response = await asyncio.wait_for(ws.recv(), timeout=10)
                listen_env = parse_envelope(listen_response)
                if listen_env.get("stream") != "listening":
                    logger.warning(
                        "Unexpected listening response: %s",
                        listen_response[:200],
                    )
            except asyncio.TimeoutError:
                logger.warning("No listening confirmation within 10s")

            async for raw_message in ws:
                try:
                    envelope = parse_envelope(raw_message)
                except json.JSONDecodeError:
                    logger.warning("Malformed websocket message: %r", raw_message)
                    continue

                if not is_trade_update(envelope):
                    continue

                try:
                    fill = parse_fill_event(envelope)
                except ValueError as exc:
                    logger.warning("Skipping malformed fill event: %s", exc)
                    continue

                if fill is None:
                    continue

                if fill.execution_id in seen:
                    logger.debug(
                        "Duplicate fill %s ignored", fill.execution_id
                    )
                    continue

                buffered.append(fill)

                if len(buffered) >= MAX_BUFFERED_FILLS:
                    async with async_session() as db:
                        buffered, seen, last_flushed_ts = await _flush_buffered(
                            db, buffered, seen, last_flushed_ts
                        )
                    continue

                async with async_session() as db:
                    buffered, seen, last_flushed_ts = await _flush_buffered(
                        db, buffered, seen, last_flushed_ts
                    )
    except websockets.ConnectionClosed:
        logger.info("Alpaca paper stream connection closed")
    except FillTrackerAuthError:
        raise
    except Exception:
        logger.exception("Unexpected error in fill tracker")
        raise
    finally:
        if buffered:
            async with async_session() as db:
                buffered, seen, last_flushed_ts = await _flush_buffered(
                    db, buffered, seen, last_flushed_ts
                )
