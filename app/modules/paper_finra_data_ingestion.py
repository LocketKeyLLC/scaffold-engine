# app/modules/paper_finra_data_ingestion.py
"""FINRA short interest and ATS volume data ingestion for paper trading.

Downloads FINRA's daily short-volume file (the regsho directory) and the
weekly per-symbol ATS totals (FINRA's public Query API), stores each record
in `ingested_item`, and creates a `finra_context` signal for every symbol
with non-zero volume data.

The HTTP operations are injected (`http_get`, `http_post`) so tests can use
a fake response without touching the network.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, Sequence

import httpx
from sqlalchemy import text

from app.database import async_session

logger = logging.getLogger("scaffold")

# Fixed URLs. The `{date}` placeholder is replaced with `YYYYMMDD`.
FINRA_SHORT_INTEREST_URL: str = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date}.txt"

#: §17.2044 — FINRA serves no weekly ATS *file*: the URL this module built, cdn.finra.org/ats/weekly/ATSvolume<date>.txt,
#: answers 403 (2026-11-26). Per-symbol weekly ATS totals are FINRA's public Query API (no key), summary type
#: ATS_W_SMBL, published about three weeks after the week (the week of 2026-11-02 appeared 2026-11-23), so a run asks for
#: the last ATS_WEEKLY_LOOKBACK_DAYS and stores the weeks it has not stored yet.
FINRA_ATS_WEEKLY_API_URL: str = "https://api.finra.org/data/group/otcMarket/name/weeklySummary"
ATS_WEEKLY_SUMMARY_TYPE: str = "ATS_W_SMBL"
ATS_WEEKLY_LOOKBACK_DAYS: int = 35
ATS_WEEKLY_PAGE_SIZE: int = 5000
ATS_WEEKLY_FIELDS: tuple[str, ...] = (
    "weekStartDate", "issueSymbolIdentifier", "totalWeeklyShareQuantity", "totalWeeklyTradeCount",
    "tierIdentifier", "lastUpdateDate",
)

FINRA_SOURCE: str = "finra"
SUPPORTED_SOURCES: frozenset[str] = frozenset({"short_interest", "ats_volume"})


class HttpGet(Protocol):
    """Protocol for an async HTTP GET callable returning a response-like object."""

    async def __call__(self, url: str) -> Any:
        ...


class HttpPost(Protocol):
    """Protocol for an async HTTP POST callable (httpx's ``client.post``) returning a response-like object."""

    async def __call__(self, url: str, *, json: Any, headers: dict[str, str]) -> Any:
        ...


@dataclass(frozen=True)
class FinraFile:
    """A FINRA data file identified by source, release date, and version."""

    source: str
    release_date: date
    version: int

    @property
    def url(self) -> str:
        return _build_url(self.source, self.release_date, self.version)


class FinraRecord:
    """A parsed FINRA pipe-delimited record.

    The first four positional arguments are fixed; any additional keyword
    arguments are treated as field values and included in the payload.
    """

    def __init__(
        self,
        source_type: str,
        release_date: date,
        version: int,
        symbol: str,
        **fields: str,
    ) -> None:
        self.source_type = source_type
        self.release_date = release_date
        self.version = version
        self.symbol = symbol
        self.fields = fields

    def payload(self) -> dict[str, Any]:
        """Return the JSON-serialisable payload for this record."""
        return {
            "source_type": self.source_type,
            "release_date": self.release_date.isoformat(),
            "version": self.version,
            "symbol": self.symbol,
            **self.fields,
        }


def _build_url(source: str, release_date: date, version: int) -> str:
    """Build the FINRA URL for a source and release date.

    `version` is accepted for API compatibility but does not change the
    URL. Unsupported source values raise ValueError.
    """
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"Unsupported FINRA file type: {source!r}")
    date_str = release_date.strftime("%Y%m%d")
    if source == "short_interest":
        return FINRA_SHORT_INTEREST_URL.format(date=date_str)
    if source == "ats_volume":
        raise ValueError("FINRA serves no weekly ATS file; the weekly ATS totals come from its Query API "
                         "(ingest_finra_ats_weekly)")
    raise AssertionError(f"source {source!r} passed SUPPORTED_SOURCES but has no URL")


#: §17.2043 — the daily Reg SHO file's columns as FINRA serves it (live 2026-11-26, CNMSshvol20261125.txt):
#: ``Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market``, a header row, decimal volumes, and a one-field
#: record-count trailer. The ``short_interest`` source name is kept (the schedule and the engine's contracts use it);
#: the file is daily short SALE volume, not bi-monthly short interest.
SHORT_VOLUME_COLUMNS: int = 6


def _parse_short_interest(
    row: list[str], release_date: date, version: int
) -> FinraRecord | None:
    """Parse one row of FINRA's daily short-volume file into a FinraRecord.

    Fields: trade date (YYYYMMDD), symbol, short volume, short-exempt volume, total volume, reporting markets.
    The header row, the record-count trailer and anything else not six fields wide return None.
    """
    if len(row) != SHORT_VOLUME_COLUMNS:
        return None
    trade_date, symbol, short_volume, short_exempt_volume, total_volume, market = [
        cell.strip() for cell in row
    ]
    if not symbol or not (len(trade_date) == 8 and trade_date.isdigit()):
        return None
    return FinraRecord(
        source_type="short_interest",
        release_date=release_date,
        version=version,
        symbol=symbol,
        trade_date=trade_date,
        short_volume=short_volume,
        short_exempt_volume=short_exempt_volume,
        total_volume=total_volume,
        market=market,
    )


def _parse_ats_weekly(item: Any, version: int) -> FinraRecord | None:
    """Parse one ``weeklySummary`` record (summary type ATS_W_SMBL) into a FinraRecord.

    The record's week (``weekStartDate``) is its release date; the totals are the symbol's ATS shares and trades for
    that week across every ATS.
    """
    if not isinstance(item, dict):
        return None
    symbol = str(item.get("issueSymbolIdentifier") or "").strip()
    week = str(item.get("weekStartDate") or "").strip()
    try:
        week_start = date.fromisoformat(week)
    except ValueError:
        return None
    if not symbol:
        return None
    return FinraRecord(
        source_type="ats_volume",
        release_date=week_start,
        version=version,
        symbol=symbol,
        week_start_date=week,
        ats_volume=str(item.get("totalWeeklyShareQuantity", "")),
        trade_count=str(item.get("totalWeeklyTradeCount", "")),
        tier=str(item.get("tierIdentifier") or ""),
        last_update_date=str(item.get("lastUpdateDate") or ""),
    )


def _parse_row(
    row: list[str], source: str, release_date: date, version: int
) -> FinraRecord | None:
    if source == "short_interest":
        return _parse_short_interest(row, release_date, version)
    return None


def _direction_for(record: FinraRecord) -> str:
    """Return 'flat' when every volume field is zero, otherwise 'long'."""
    numeric_keys: tuple[str, ...]
    if record.source_type == "short_interest":
        numeric_keys = ("short_volume", "short_exempt_volume", "total_volume")
    else:  # ats_volume
        numeric_keys = ("ats_volume", "trade_count")

    values: list[Decimal] = []
    for key in numeric_keys:
        raw = record.fields.get(key, "")
        try:
            value = Decimal(raw or "0")
        except (InvalidOperation, ValueError):
            continue
        values.append(value)

    if values and all(v == 0 for v in values):
        return "flat"
    return "long"


async def ingest_finra_file(
    *,
    source: str,
    release_date: date,
    version: int,
    http_get: HttpGet,
) -> dict[str, int]:
    """Download and ingest one FINRA file.

    Returns `{"ingested_items": int, "signals": int}`. Each record is
    inserted only once (idempotent); malformed rows are skipped silently.
    """
    if source not in SUPPORTED_SOURCES:
        raise ValueError(f"Unsupported FINRA file type: {source!r}")

    url = _build_url(source, release_date, version)
    response = await http_get(url)
    response.raise_for_status()

    content = response.text
    if not content.strip():
        return {"ingested_items": 0, "signals": 0}

    rows = list(csv.reader(io.StringIO(content), delimiter="|"))
    records = []
    for row in rows:
        if not row or all(not cell.strip() for cell in row):
            continue
        record = _parse_row(row, source, release_date, version)
        if record is not None:
            records.append(record)
    return await _store_records(records)


async def _store_records(records: Sequence[FinraRecord]) -> dict[str, int]:
    """Store each record once (keyed by its date, symbol, source and version) with its `finra_context` signal."""
    counts = {"ingested_items": 0, "signals": 0}
    if not records:
        return counts

    async with async_session() as db:
        for record in records:
            source = record.source_type
            payload = record.payload()
            external_id = f"{record.release_date.isoformat()}:{record.symbol}:{source}:{record.version}"
            published_ts = datetime.combine(
                record.release_date, datetime.min.time(), tzinfo=timezone.utc
            )

            result = await db.execute(
                text(
                    "INSERT INTO ingested_item "
                    "(source, external_id, published_ts, fetched_ts, payload) "
                    "VALUES (:source, :external_id, :published_ts, now(), CAST(:payload AS json)) "
                    "ON CONFLICT (source, external_id) DO NOTHING "
                    "RETURNING id"
                ),
                {
                    "source": FINRA_SOURCE,
                    "external_id": external_id,
                    "published_ts": published_ts,
                    "payload": json.dumps(payload),
                },
            )
            inserted_id = result.scalar_one_or_none()
            if inserted_id is None:
                continue

            counts["ingested_items"] += 1
            direction = _direction_for(record)
            await db.execute(
                text(
                    "INSERT INTO signal "
                    "(ingested_item_id, ticker, signal_type, direction, rationale) "
                    "VALUES (:ingested_item_id, :ticker, 'finra_context', :direction, :rationale)"
                ),
                {
                    "ingested_item_id": inserted_id,
                    "ticker": record.symbol,
                    "direction": direction,
                    "rationale": f"FINRA {source} for {record.release_date.isoformat()}",
                },
            )
            counts["signals"] += 1

        await db.commit()

    return counts


async def ingest_finra_files(
    *,
    source: str,
    release_dates: Sequence[date],
    http_get: HttpGet,
) -> dict[str, int]:
    """Ingest multiple FINRA files and aggregate the counts."""
    total = {"ingested_items": 0, "signals": 0}
    for release_date in release_dates:
        counts = await ingest_finra_file(
            source=source,
            release_date=release_date,
            version=1,
            http_get=http_get,
        )
        total["ingested_items"] += counts["ingested_items"]
        total["signals"] += counts["signals"]
    return total


def ats_weekly_query(symbols: Sequence[str], until: date, *, offset: int = 0) -> dict[str, Any]:
    """The ``weeklySummary`` request body: ATS_W_SMBL rows for ``symbols`` whose week starts in the lookback window."""
    since = until - timedelta(days=ATS_WEEKLY_LOOKBACK_DAYS)
    return {
        "limit": ATS_WEEKLY_PAGE_SIZE,
        "offset": offset,
        "fields": list(ATS_WEEKLY_FIELDS),
        "compareFilters": [
            {"compareType": "EQUAL", "fieldName": "summaryTypeCode", "fieldValue": ATS_WEEKLY_SUMMARY_TYPE},
        ],
        "dateRangeFilters": [
            {"fieldName": "weekStartDate", "startDate": since.isoformat(), "endDate": until.isoformat()},
        ],
        "domainFilters": [{"fieldName": "issueSymbolIdentifier", "values": sorted(set(symbols))}],
    }


async def ingest_finra_ats_weekly(
    *,
    symbols: Sequence[str],
    until: date,
    http_post: HttpPost,
) -> dict[str, int]:
    """§17.2044 — store the weekly ATS totals FINRA has published for ``symbols`` in the lookback window.

    Pages through FINRA's Query API until a page comes back short; a week already stored is not stored twice.
    Returns ``{"ingested_items": int, "signals": int}``.
    """
    records: list[FinraRecord] = []
    offset = 0
    while True:
        response = await http_post(
            FINRA_ATS_WEEKLY_API_URL,
            json=ats_weekly_query(symbols, until, offset=offset),
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        page = response.json() or []
        for item in page:
            record = _parse_ats_weekly(item, version=1)
            if record is not None:
                records.append(record)
        if len(page) < ATS_WEEKLY_PAGE_SIZE:
            break
        offset += ATS_WEEKLY_PAGE_SIZE
    return await _store_records(records)
