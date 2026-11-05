# tests/test_paper_signal_generator.py
"""Tests for app.modules.paper_signal_generator."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_audit_trail_writer import write_signal
from app.modules.paper_signal_generator import (
    generate_signals,
    normalize_congressional_raw_dict,
    normalize_edgar_payload,
)

pytestmark = pytest.mark.asyncio


async def _insert_ingested_item(
    db,
    *,
    source: str,
    external_id: str,
    published_ts: datetime,
    payload: dict,
) -> int:
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
            "external_id": external_id,
            "published_ts": published_ts,
            "payload": json.dumps(payload),
        },
    )
    return result.scalar_one()


async def test_generate_signals_returns_contract_fields_with_signal_id():
    """Every returned signal carries the shared contract keys plus signal_id."""
    async with async_session() as db:
        item_id = await _insert_ingested_item(
            db,
            source="news",
            external_id=uuid4().hex,
            published_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
            payload={"ticker": "AAA", "title": "test"},
        )
        await db.commit()

        signals = await generate_signals(
            datetime(2026, 1, 2, tzinfo=timezone.utc), db=db
        )

    assert len(signals) == 1
    signal = signals[0]
    assert set(signal) == {
        "source",
        "ticker",
        "signal_type",
        "direction",
        "rationale",
        "published_ts",
        "signal_id",
    }
    assert signal["signal_id"] is not None


async def test_generate_signals_orders_by_source_weight_descending():
    """Signals are ordered by source weight descending."""
    async with async_session() as db:
        await _insert_ingested_item(
            db,
            source="reddit",
            external_id=uuid4().hex,
            published_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
            payload={"ticker": "RED", "title": "r"},
        )
        await _insert_ingested_item(
            db,
            source="sec_edgar",
            external_id=uuid4().hex,
            published_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
            payload={
                "ticker": "EDG",
                "accession_number": "0001",
                "filing_date": "2026-01-01",
            },
        )
        await db.commit()

        signals = await generate_signals(
            datetime(2026, 1, 2, tzinfo=timezone.utc), db=db
        )

    assert [s["source"] for s in signals] == ["sec_edgar", "reddit"]


async def test_generate_signals_orders_by_id_within_source():
    """Within a source, insertion order (id) is preserved."""
    async with async_session() as db:
        await _insert_ingested_item(
            db,
            source="news",
            external_id=uuid4().hex,
            published_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
            payload={"ticker": "N1"},
        )
        await _insert_ingested_item(
            db,
            source="news",
            external_id=uuid4().hex,
            published_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
            payload={"ticker": "N2"},
        )
        await db.commit()

        signals = await generate_signals(
            datetime(2026, 1, 2, tzinfo=timezone.utc), db=db
        )

    assert [s["ticker"] for s in signals] == ["N1", "N2"]


async def test_generate_signals_filters_by_published_ts():
    """Items published after the timestamp are excluded."""
    async with async_session() as db:
        await _insert_ingested_item(
            db,
            source="news",
            external_id=uuid4().hex,
            published_ts=datetime(2026, 1, 3, tzinfo=timezone.utc),
            payload={"ticker": "FUT"},
        )
        await db.commit()

        signals = await generate_signals(
            datetime(2026, 1, 2, tzinfo=timezone.utc), db=db
        )

    assert signals == []


async def test_generate_signals_uses_own_session_when_db_none():
    """When db is None, the generator reads via its own async_session."""
    async with async_session() as db:
        await _insert_ingested_item(
            db,
            source="news",
            external_id=uuid4().hex,
            published_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
            payload={"ticker": "OWN"},
        )
        await db.commit()

    signals = await generate_signals(datetime(2026, 1, 2, tzinfo=timezone.utc))

    assert len(signals) == 1
    assert signals[0]["ticker"] == "OWN"


async def test_generate_signals_skips_empty_ticker():
    """Items whose normalized ticker is empty are skipped and not saved."""
    async with async_session() as db:
        await _insert_ingested_item(
            db,
            source="news",
            external_id=uuid4().hex,
            published_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
            payload={"title": "no ticker"},
        )
        await db.commit()

        signals = await generate_signals(
            datetime(2026, 1, 2, tzinfo=timezone.utc), db=db
        )

    assert signals == []


async def test_generate_signals_idempotent_save():
    """A second call reuses the existing signal id and writes no second row."""
    async with async_session() as db:
        item_id = await _insert_ingested_item(
            db,
            source="news",
            external_id=uuid4().hex,
            published_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
            payload={"ticker": "IDM"},
        )
        await db.commit()

        first = await generate_signals(
            datetime(2026, 1, 2, tzinfo=timezone.utc), db=db
        )
        second = await generate_signals(
            datetime(2026, 1, 2, tzinfo=timezone.utc), db=db
        )

        count = await db.execute(
            text("SELECT COUNT(*) FROM signal WHERE ingested_item_id = :iid"),
            {"iid": item_id},
        )
        signal_count = count.scalar_one()

    assert len(first) == 1
    assert len(second) == 1
    assert first[0]["signal_id"] == second[0]["signal_id"]
    assert signal_count == 1


async def test_generate_signals_reuses_existing_signal_id():
    """A pre-existing signal row is reused; no duplicate is written."""
    async with async_session() as db:
        item_id = await _insert_ingested_item(
            db,
            source="news",
            external_id=uuid4().hex,
            published_ts=datetime(2026, 1, 1, tzinfo=timezone.utc),
            payload={"ticker": "REU"},
        )
        await db.commit()

        existing_id = await write_signal(
            ingested_item_id=item_id,
            ticker="REU",
            signal_type="news_context",
            direction="long",
            rationale="pre-seeded",
        )
        await db.commit()

        signals = await generate_signals(
            datetime(2026, 1, 2, tzinfo=timezone.utc), db=db
        )

        count = await db.execute(
            text("SELECT COUNT(*) FROM signal WHERE ingested_item_id = :iid"),
            {"iid": item_id},
        )
        signal_count = count.scalar_one()

    assert len(signals) == 1
    assert signals[0]["signal_id"] == existing_id
    assert signal_count == 1


async def test_generate_signals_empty_when_no_items():
    """An empty ingested_item table yields an empty signal list."""
    async with async_session() as db:
        signals = await generate_signals(
            datetime(2026, 1, 2, tzinfo=timezone.utc), db=db
        )

    assert signals == []


def test_normalize_edgar_payload_contract():
    """normalize_edgar_payload returns the shared contract without signal_id."""
    payload = {
        "ticker": "AAPL",
        "accession_number": "0001",
        "filing_date": "2026-01-01",
    }
    result = normalize_edgar_payload(payload)

    assert set(result) == {
        "source",
        "ticker",
        "signal_type",
        "direction",
        "rationale",
        "published_ts",
    }
    assert result["ticker"] == "AAPL"
    assert result["source"] == "sec_edgar"
    assert result["signal_type"] == "form4_purchase"


def test_normalize_edgar_payload_missing_ticker_is_empty():
    """A sec_edgar payload without a ticker key normalizes to empty ticker."""
    payload = {
        "accession_number": "0001",
        "filing_date": "2026-01-01",
    }
    result = normalize_edgar_payload(payload)

    assert result["ticker"] == ""


def test_normalize_congressional_raw_dict_contract():
    """normalize_congressional_raw_dict returns the shared contract without signal_id."""
    raw = {
        "ticker": "MSFT",
        "transaction_type": "P",
        "published_ts": "2026-01-01T00:00:00Z",
    }
    result = normalize_congressional_raw_dict(raw)

    assert set(result) == {
        "source",
        "ticker",
        "signal_type",
        "direction",
        "rationale",
        "published_ts",
    }
    assert result["ticker"] == "MSFT"
    assert result["source"] == "congress"
    assert result["signal_type"] == "congress_trade"


def test_normalize_congressional_sale_is_flat():
    """A congressional sale normalizes to direction 'flat'."""
    raw = {
        "ticker": "MSFT",
        "transaction_type": "S",
        "published_ts": "2026-01-01T00:00:00Z",
    }
    result = normalize_congressional_raw_dict(raw)

    assert result["direction"] == "flat"
