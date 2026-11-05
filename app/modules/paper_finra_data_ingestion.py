# app/modules/paper_finra_data_ingestion.py
"""FINRA short interest and ATS volume data ingestion for paper trading.

Downloads pipe-delimited FINRA files (short interest from the daily
regsho directory, ATS volume from the weekly directory), parses each
record, stores it in `ingested_item`, and creates a `finra_context`
signal for every symbol with non-zero volume data.

The HTTP GET operation is injected as `http_get` so tests can use a
fake response without touching the network.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol, Sequence

import httpx
from sqlalchemy import text

from app.database import async_session

logger = logging.getLogger("scaffold")

# Fixed URLs. The `{date}` placeholder is replaced with `YYYYMMDD`.
FINRA_SHORT_INTEREST_URL: str = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol{date}.txt"
FINRA_ATS_VOLUME_URL: str = "https://cdn.finra.org/ats/weekly/ATSvolume{date}.txt"

FINRA_SOURCE: str = "finra"
SUPPORTED_SOURCES: frozenset[str] = frozenset({"short_interest", "ats_volume"})


class HttpGet(Protocol):
    """Protocol for an async HTTP GET callable returning a response-like object."""

    async def __call__(self, url: str) -> Any:
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
        return FINRA_ATS_VOLUME_URL.format(date=date_str)
    raise AssertionError(f"source {source!r} passed SUPPORTED_SOURCES but has no URL")


def _parse_short_interest(
    row: list[str], release_date: date, version: int
) -> FinraRecord | None:
    """Parse a 5-field short interest row into a FinraRecord.

    Expected fields: settlement date, symbol, short interest,
    average daily volume, days to cover.
    """
    if len(row) != 5:
        return None
    settlement_date, symbol, short_interest, avg_daily_volume, days_to_cover = [
        cell.strip() for cell in row
    ]
    if not symbol:
        return None
    return FinraRecord(
        source_type="short_interest",
        release_date=release_date,
        version=version,
        symbol=symbol,
        settlement_date=settlement_date,
        short_interest=short_interest,
        avg_daily_volume=avg_daily_volume,
        days_to_cover=days_to_cover,
    )


def _parse_ats_volume(
    row: list[str], release_date: date, version: int
) -> FinraRecord | None:
    """Parse a 5-field ATS volume row into a FinraRecord.

    Expected fields: week end date, symbol, ATS volume,
    total volume, ATS market share.
    """
    if len(row) != 5:
        return None
    week_end_date, symbol, ats_volume, total_volume, ats_market_share = [
        cell.strip() for cell in row
    ]
    if not symbol:
        return None
    return FinraRecord(
        source_type="ats_volume",
        release_date=release_date,
        version=version,
        symbol=symbol,
        week_end_date=week_end_date,
        ats_volume=ats_volume,
        total_volume=total_volume,
        ats_market_share=ats_market_share,
    )


def _parse_row(
    row: list[str], source: str, release_date: date, version: int
) -> FinraRecord | None:
    if source == "short_interest":
        return _parse_short_interest(row, release_date, version)
    if source == "ats_volume":
        return _parse_ats_volume(row, release_date, version)
    return None


def _direction_for(record: FinraRecord) -> str:
    """Return 'flat' when every volume field is zero, otherwise 'long'."""
    numeric_keys: tuple[str, ...]
    if record.source_type == "short_interest":
        numeric_keys = ("short_interest", "avg_daily_volume", "days_to_cover")
    else:  # ats_volume
        numeric_keys = ("ats_volume", "total_volume", "ats_market_share")

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
    counts = {"ingested_items": 0, "signals": 0}

    async with async_session() as db:
        for row in rows:
            if not row or all(not cell.strip() for cell in row):
                continue
            record = _parse_row(row, source, release_date, version)
            if record is None:
                continue

            payload = record.payload()
            external_id = f"{release_date.isoformat()}:{record.symbol}:{source}:{version}"
            published_ts = datetime.combine(
                release_date, datetime.min.time(), tzinfo=timezone.utc
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
                    "rationale": f"FINRA {source} for {release_date.isoformat()}",
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
