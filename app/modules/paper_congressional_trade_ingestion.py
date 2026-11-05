# app/modules/paper_congressional_trade_ingestion.py
"""Congressional financial-disclosure ingestion for the paper-trading lab.

The research (API_CONGRESS_TRADES) confirms only the base hosts for the
House and Senate disclosure portals:

  * House: https://disclosures-clerk.house.gov
  * Senate: https://efdsearch.senate.gov

It does **not** confirm any API endpoint, response format, field list,
HTML structure, date format, pagination scheme, rate limit, or third-party
mirror.  Consequently this module stores the raw HTTP responses as
``ingested_item`` rows with ``source = 'congress'`` and does not fabricate
structured fields such as ``filing_id``, ``asset_ticker``, or
``transaction_type``.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from app.database import async_session
from sqlalchemy import text

logger = logging.getLogger("scaffold")

# Verified base hosts from API_CONGRESS_TRADES research.
HOUSE_BASE_URL: str = "https://disclosures-clerk.house.gov"
SENATE_BASE_URL: str = "https://efdsearch.senate.gov"

# Source value used in the audit trail.
CONGRESS_SOURCE: str = "congress"

# Known Senate legal notice.  The research documents the restriction but
# does not describe a programmatic acceptance flow, so we record the notice
# with any Senate fetch rather than bypassing it.
SENATE_LEGAL_NOTICE: str = (
    "Use of Senate eFD search data is restricted by 5 U.S.C. app. § 105(c): "
    "no commercial use, no credit rating, no solicitation, $10,000 penalty."
)


class CongressionalIngestionError(Exception):
    """Raised when Congressional disclosure ingestion fails irrecoverably."""


@dataclass(frozen=True)
class DisclosureRow:
    """A raw disclosure item as returned by ``fetch_disclosures``.

    This dataclass intentionally carries only the fields the ingestion
    code can determine without guessing the response format: the chamber,
    the fetch URL, the raw response body, a publication timestamp inferred
    from HTTP headers, and a deterministic surrogate external id.
    """

    chamber: str
    url: str
    raw_response: str
    published_ts: datetime | None
    external_id: str


@dataclass(frozen=True)
class FetchResult:
    """Result of a disclosure fetch attempt."""

    rows: list[DisclosureRow]
    errors: list[str]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _guess_published_ts(response: httpx.Response) -> datetime | None:
    """Infer a publication timestamp from HTTP headers only.

    The research documents no date format in the response body, so we do
    not attempt to parse HTML.  We use the ``Last-Modified`` header if
    present, otherwise ``Date``.
    """
    for header in ("last-modified", "date"):
        value = response.headers.get(header)
        if not value:
            continue
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc)
        except (ValueError, TypeError, OverflowError):
            continue
    return None


def _external_id_for(chamber: str, url: str) -> str:
    """Build a stable, deterministic surrogate external id for a fetch."""
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return f"{chamber}:{digest}"


async def _store_row(db: Any, row: DisclosureRow) -> int:
    """Insert one raw disclosure row into ``ingested_item``.

    Uses ``ON CONFLICT DO NOTHING`` so repeated fetches of the same URL
    are idempotent.
    """
    payload = {
        "chamber": row.chamber,
        "url": row.url,
        "raw_response": row.raw_response,
        "legal_notice": SENATE_LEGAL_NOTICE if row.chamber == "senate" else None,
    }
    result = await db.execute(
        text("""
            INSERT INTO ingested_item (
                source, external_id, published_ts, fetched_ts, payload, model_ts, data_ts
            ) VALUES (
                :source, :external_id, :published_ts, :fetched_ts,
                CAST(:payload AS json), :model_ts, :data_ts
            )
            ON CONFLICT (source, external_id) DO NOTHING
            RETURNING id
        """),
        {
            "source": CONGRESS_SOURCE,
            "external_id": row.external_id,
            "published_ts": row.published_ts or _now(),
            "fetched_ts": _now(),
            "payload": json.dumps(payload),
            "model_ts": _now(),
            "data_ts": _now(),
        },
    )
    row_id = result.scalar_one_or_none()
    if row_id is None:
        existing = await db.execute(
            text("SELECT id FROM ingested_item WHERE source = :source AND external_id = :external_id"),
            {"source": CONGRESS_SOURCE, "external_id": row.external_id},
        )
        row_id = existing.scalar_one()
    return row_id


async def fetch_disclosures(
    client: httpx.AsyncClient,
    *,
    max_requests: int = 10,
) -> FetchResult:
    """Fetch raw House/Senate disclosure pages and store them.

    Because the research documents no machine-readable format, this
    function records the raw HTTP response bodies.  It does not parse
    HTML or assert field names that are not confirmed by the research.

    Args:
        client: An ``httpx.AsyncClient`` to use for HTTP requests.
        max_requests: Maximum number of HTTP requests to make.  The research
            names no pagination or request-counting behavior for the
            disclosure sources, so this is a caller-controlled safety cap.

    Returns:
        A ``FetchResult`` containing stored rows and any errors.
    """
    rows: list[DisclosureRow] = []
    errors: list[str] = []

    urls = [
        ("house", f"{HOUSE_BASE_URL}/FinancialDisclosure"),
        ("senate", f"{SENATE_BASE_URL}/search/home/"),
    ]

    async with async_session() as db:
        for chamber, url in urls:
            if len(rows) + len(errors) >= max_requests:
                errors.append(f"Reached max_requests ({max_requests}) before fetching {chamber}")
                break
            try:
                response = await client.get(url, timeout=30.0)
                response.raise_for_status()
            except httpx.HTTPError as exc:
                errors.append(f"{chamber} fetch failed: {exc}")
                continue

            row = DisclosureRow(
                chamber=chamber,
                url=url,
                raw_response=response.text,
                published_ts=_guess_published_ts(response),
                external_id=_external_id_for(chamber, url),
            )

            try:
                await _store_row(db, row)
                rows.append(row)
            except Exception as exc:
                errors.append(f"{chamber} store failed: {exc}")
                logger.exception("Failed to store congressional disclosure row")

        await db.commit()

    return FetchResult(rows=rows, errors=errors)
