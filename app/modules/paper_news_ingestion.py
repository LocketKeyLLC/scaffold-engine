# app/modules/paper_news_ingestion.py
"""News ingestion module for the paper trading pipeline.

Fetches news items via SearXNG, stores them in the ingested_item table,
and writes audit trail entries for ticker-relevant news signals.
"""

import json
import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from app.database import async_session
from app.modules.paper_audit_trail_writer import write_signal

logger = logging.getLogger("scaffold")

# Fixed search configuration (module constants, not environment lookups)
NEWS_SOURCES = ("news",)
SEARXNG_NEWS_CATEGORY = "news"
DEFAULT_MAX_RESULTS = 20


async def fetch_news_items(
    query: str,
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    ticker: Optional[str] = None,
) -> list[dict[str, Any]]:
    """Fetch news items from SearXNG for the given query.

    Args:
        query: Search query string.
        max_results: Maximum number of results to return.
        ticker: Optional ticker symbol to filter/annotate results.

    Returns:
        List of result dicts with keys: title, url, content, published_ts,
        source, external_id.
    """
    from app.modules.gt_extractor import search_searxng

    results = await search_searxng(query, max_results)

    items: list[dict[str, Any]] = []
    for result in results:
        title = result.get("title", "")
        url = result.get("url", "")
        content = result.get("content", "")
        published_ts = result.get("publishedDate") or result.get("published_ts")

        if not title or not url:
            logger.warning("Skipping news result with missing title/url: %r", result)
            continue

        # Parse publication time; default to now if unavailable
        if published_ts:
            try:
                if isinstance(published_ts, str):
                    # Handle ISO 8601 format
                    parsed_ts = datetime.fromisoformat(
                        published_ts.replace("Z", "+00:00")
                    )
                    if parsed_ts.tzinfo is None:
                        parsed_ts = parsed_ts.replace(tzinfo=timezone.utc)
                elif isinstance(published_ts, datetime):
                    parsed_ts = published_ts
                    if parsed_ts.tzinfo is None:
                        parsed_ts = parsed_ts.replace(tzinfo=timezone.utc)
                else:
                    parsed_ts = datetime.now(timezone.utc)
            except (ValueError, TypeError):
                logger.warning(
                    "Could not parse published_ts %r for %s; using now",
                    published_ts,
                    url,
                )
                parsed_ts = datetime.now(timezone.utc)
        else:
            parsed_ts = datetime.now(timezone.utc)

        items.append(
            {
                "title": title,
                "url": url,
                "content": content,
                "published_ts": parsed_ts,
                "source": "news",
                "external_id": url,
            }
        )

    return items


async def store_news_item(
    *,
    source: str,
    external_id: str,
    published_ts: datetime,
    payload: dict[str, Any],
    db: Optional[Any] = None,
) -> int:
    """Store a news item in the ingested_item table.

    Args:
        source: Source identifier (must be 'news' per schema constraint).
        external_id: External identifier (URL).
        published_ts: Publication timestamp.
        payload: JSON payload with item details.
        db: Optional database session; if None, creates one from async_session.

    Returns:
        The inserted row's id.

    Raises:
        ValueError: If source is not 'news' or published_ts lacks timezone.
    """
    if source != "news":
        raise ValueError(f"Invalid source: {source!r}; must be 'news'")
    if published_ts.tzinfo is None:
        raise ValueError("published_ts must be timezone-aware")

    own_session = db is None
    session = db if db is not None else async_session()

    try:
        result = await session.execute(
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
        row = result.fetchone()
        if row is None:
            raise RuntimeError("INSERT INTO ingested_item returned no id")
        await session.commit()
        return int(row[0])
    finally:
        if own_session:
            await session.close()


async def ingest_news_for_ticker(
    ticker: str,
    *,
    query: Optional[str] = None,
    max_results: int = DEFAULT_MAX_RESULTS,
    write_signals: bool = True,
    db: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Fetch and store news items for a ticker, optionally writing signals.

    Args:
        ticker: Ticker symbol.
        query: Optional custom query; defaults to f"{ticker} stock news".
        max_results: Maximum results to fetch.
        write_signals: Whether to write signal audit entries.
        db: Optional database session.

    Returns:
        List of stored item dicts with 'ingested_item_id' key.
    """
    search_query = query or f"{ticker} stock news"
    items = await fetch_news_items(search_query, max_results=max_results, ticker=ticker)

    stored_items: list[dict[str, Any]] = []
    for item in items:
        try:
            ingested_item_id = await store_news_item(
                source=item["source"],
                external_id=item["external_id"],
                published_ts=item["published_ts"],
                payload={
                    "title": item["title"],
                    "url": item["url"],
                    "content": item["content"],
                    "ticker": ticker,
                },
                db=db,
            )
            item["ingested_item_id"] = ingested_item_id
            stored_items.append(item)

            if write_signals:
                await write_signal(
                    ingested_item_id=ingested_item_id,
                    ticker=ticker,
                    signal_type="news_context",
                    direction="flat",
                    rationale=f"News item: {item['title'][:200]}",
                )
        except Exception as exc:
            logger.error(
                "Failed to store news item for ticker %s: %s",
                ticker,
                exc,
                exc_info=True,
            )
            continue

    return stored_items


async def ingest_news_batch(
    tickers: list[str],
    *,
    max_results: int = DEFAULT_MAX_RESULTS,
    write_signals: bool = True,
    db: Optional[Any] = None,
) -> dict[str, list[dict[str, Any]]]:
    """Ingest news for multiple tickers.

    Args:
        tickers: List of ticker symbols.
        max_results: Maximum results per ticker.
        write_signals: Whether to write signal audit entries.
        db: Optional database session.

    Returns:
        Dict mapping ticker to list of stored items.
    """
    results: dict[str, list[dict[str, Any]]] = {}
    for ticker in tickers:
        try:
            items = await ingest_news_for_ticker(
                ticker,
                max_results=max_results,
                write_signals=write_signals,
                db=db,
            )
            results[ticker] = items
        except Exception as exc:
            logger.error(
                "Failed to ingest news for ticker %s: %s",
                ticker,
                exc,
                exc_info=True,
            )
            results[ticker] = []
    return results
