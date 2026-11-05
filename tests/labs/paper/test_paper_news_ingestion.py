# tests/test_paper_news_ingestion.py
"""Tests for paper_news_ingestion."""

import json
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_news_ingestion import (
    fetch_news_items,
    ingest_news_batch,
    ingest_news_for_ticker,
    store_news_item,
)


def _aware(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _insert_ingested_item(
    db, *, source: str, external_id: str, published_ts: datetime, payload: dict
) -> int:
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
            "external_id": external_id,
            "published_ts": published_ts,
            "payload": json.dumps(payload),
        },
    )
    await db.commit()
    return int(result.fetchone()[0])


async def test_store_news_item_inserts_row():
    published_ts = _aware(datetime(2026, 10, 25, 12, 0, 0))
    payload = {
        "title": "T",
        "url": f"https://example.com/{uuid4().hex}",
        "content": "C",
        "ticker": "AAPL",
    }

    async with async_session() as db:
        rid = await store_news_item(
            source="news",
            external_id=payload["url"],
            published_ts=published_ts,
            payload=payload,
            db=db,
        )

        result = await db.execute(
            text("SELECT source, external_id, published_ts, payload FROM ingested_item WHERE id = :id"),
            {"id": rid},
        )
        row = result.fetchone()
        assert row is not None
        assert row[0] == "news"
        assert row[1] == payload["url"]
        assert row[2] == published_ts
        assert row[3] == payload


async def test_store_news_item_rejects_bad_source():
    with pytest.raises(ValueError, match="Invalid source"):
        await store_news_item(
            source="sec_edgar",
            external_id="x",
            published_ts=_aware(datetime.now(timezone.utc)),
            payload={},
        )


async def test_store_news_item_rejects_naive_ts():
    with pytest.raises(ValueError, match="timezone-aware"):
        await store_news_item(
            source="news",
            external_id="x",
            published_ts=datetime(2026, 10, 25, 12, 0, 0),
            payload={},
        )


async def test_ingest_news_for_ticker_writes_signal(monkeypatch):
    ticker = "AAPL"
    url = f"https://example.com/{uuid4().hex}"
    published_ts = _aware(datetime(2026, 10, 25, 12, 0, 0))

    async def fake_fetch(query, max_results=None, ticker=None):
        return [
            {
                "title": "News",
                "url": url,
                "content": "Body",
                "published_ts": published_ts,
                "source": "news",
                "external_id": url,
            }
        ]

    monkeypatch.setattr(
        "app.modules.paper_news_ingestion.fetch_news_items", fake_fetch
    )

    async with async_session() as db:
        items = await ingest_news_for_ticker(ticker, db=db)
        assert len(items) == 1
        assert items[0]["ingested_item_id"] > 0

        result = await db.execute(
            text("SELECT ticker, signal_type, direction FROM signal WHERE ingested_item_id = :iid"),
            {"iid": items[0]["ingested_item_id"]},
        )
        row = result.fetchone()
        assert row is not None
        assert row[0] == ticker
        assert row[1] == "news_context"
        assert row[2] == "flat"


async def test_ingest_news_for_ticker_no_signals(monkeypatch):
    ticker = "MSFT"
    url = f"https://example.com/{uuid4().hex}"
    published_ts = _aware(datetime(2026, 10, 25, 12, 0, 0))

    async def fake_fetch(query, max_results=None, ticker=None):
        return [
            {
                "title": "News",
                "url": url,
                "content": "Body",
                "published_ts": published_ts,
                "source": "news",
                "external_id": url,
            }
        ]

    monkeypatch.setattr(
        "app.modules.paper_news_ingestion.fetch_news_items", fake_fetch
    )

    async with async_session() as db:
        items = await ingest_news_for_ticker(ticker, write_signals=False, db=db)
        assert len(items) == 1

        result = await db.execute(
            text("SELECT COUNT(*) FROM signal WHERE ingested_item_id = :iid"),
            {"iid": items[0]["ingested_item_id"]},
        )
        assert result.fetchone()[0] == 0


async def test_ingest_news_batch(monkeypatch):
    tickers = ["AAPL", "MSFT"]
    published_ts = _aware(datetime(2026, 10, 25, 12, 0, 0))

    async def fake_fetch(query, max_results=None, ticker=None):
        url = f"https://example.com/{uuid4().hex}"
        return [
            {
                "title": f"News for {ticker}",
                "url": url,
                "content": "Body",
                "published_ts": published_ts,
                "source": "news",
                "external_id": url,
            }
        ]

    monkeypatch.setattr(
        "app.modules.paper_news_ingestion.fetch_news_items", fake_fetch
    )

    async with async_session() as db:
        results = await ingest_news_batch(tickers, db=db)
        assert set(results.keys()) == set(tickers)
        for ticker in tickers:
            assert len(results[ticker]) == 1
            assert results[ticker][0]["ingested_item_id"] > 0
