# app/modules/paper_signal_generator.py
"""Generate signals from point-in-time data, ordered by source weights.

Aligns ingestion payloads and signal/proposal consumers on one data
contract: every returned signal dict carries the keys downstream
consumers read — ``source``, ``ticker``, ``signal_type``, ``direction``,
``rationale``, ``published_ts``, and ``signal_id`` — regardless of which
ingestion module produced the underlying ``ingested_item`` row.

The payloads written by ``paper_edgar_form_4_ingestion`` (keys: cik,
accession_number, filing_date, ...) and
``paper_congressional_trade_ingestion`` (raw ``dict(raw)``) do not carry
``source`` or ``ticker``.  This module normalizes those payloads into
the shared contract before returning them.

Each returned signal is persisted to the ``signal`` table via
``paper_audit_trail_writer.write_signal``.  The write is idempotent: an
existing ``signal`` row for the same ``ingested_item_id`` is reused and
its ``id`` is returned as ``signal_id``; no second row is written.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text

from app.database import async_session
from app.modules.paper_audit_trail_writer import write_signal
from app.modules.paper_point_time_store import get_data_as_of

logger = logging.getLogger("scaffold")

# Source weights: higher weight = earlier in output order.
SOURCE_WEIGHTS: dict[str, int] = {
    "sec_edgar": 6,
    "13f": 5,
    "congress": 4,
    "finra": 3,
    "news": 2,
    "reddit": 1,
}

# Signal types accepted by the signal table's CHECK constraint.
VALID_SIGNAL_TYPES = frozenset(
    {
        "form4_purchase",
        "13f_holding",
        "congress_trade",
        "finra_context",
        "news_context",
        "reddit_context",
    }
)

# Directions accepted by the signal table's CHECK constraint.
VALID_DIRECTIONS = frozenset({"long", "flat"})


def _source_of(item: dict[str, Any]) -> str:
    """Return the signal source for an ingested item.

    The source is the ``source`` column of the ``ingested_item`` row.
    When the row itself lacks a ``source`` key (older payloads), the
    payload's own ``source`` key is consulted as a fallback.
    """
    source = item.get("source") or item.get("payload", {}).get("source")
    return str(source or "").lower()


def _extract_ticker(payload: dict[str, Any], source: str) -> str:
    """Extract a ticker from an ingestion payload.

    Each ingestion module writes a different payload shape.  This
    function knows the shapes written by the modules in this job and
    returns the ticker they carry, uppercased and stripped.  Returns an
    empty string when no ticker can be determined.
    """
    if source == "sec_edgar":
        # paper_edgar_form_4_ingestion writes payload keys:
        # cik, accession_number, filing_date, report_date,
        # primary_document, primary_doc_description, items,
        # document_url, document_text, and now ticker (added by T6U9).
        ticker = payload.get("ticker")
        return str(ticker or "").strip().upper()
    if source == "congress":
        # paper_congressional_trade_ingestion writes the raw record
        # dict as payload.  Its DisclosureRow normalization puts the
        # ticker under asset_ticker or ticker.
        ticker = payload.get("asset_ticker") or payload.get("ticker")
        return str(ticker or "").strip().upper()
    # Generic fallback: many payloads carry ticker or symbol.
    ticker = payload.get("ticker") or payload.get("symbol")
    return str(ticker or "").strip().upper()


def _signal_type_for(source: str) -> str:
    """Map an ingested_item source to a signal_type.

    The signal table's CHECK constraint allows only:
    form4_purchase, 13f_holding, congress_trade, finra_context,
    news_context, reddit_context.
    """
    mapping = {
        "sec_edgar": "form4_purchase",
        "13f": "13f_holding",
        "congress": "congress_trade",
        "finra": "finra_context",
        "news": "news_context",
        "reddit": "reddit_context",
    }
    return mapping.get(source, "news_context")


def _direction_for(source: str, payload: dict[str, Any]) -> str:
    """Determine the signal direction for a payload.

    Congressional sales are ``flat`` (paper-only; no shorting); all
    other sources default to ``long``.
    """
    if source == "congress":
        transaction_type = str(
            payload.get("transaction_type") or payload.get("tx_type") or ""
        ).strip().upper()
        if transaction_type.startswith("S"):
            return "flat"
    return "long"


def _rationale_for(source: str, payload: dict[str, Any], ticker: str) -> str:
    """Build a rationale string for the signal.

    Kept deterministic and free of model calls so the signal generator
    can run in a sandbox with no network or secrets.
    """
    if source == "sec_edgar":
        accession = payload.get("accession_number") or ""
        return f"SEC Form 4 filing {accession}"
    if source == "congress":
        member = payload.get("member_name") or payload.get("filer_name") or ""
        asset = ticker or payload.get("asset_name") or "unknown asset"
        return f"Congressional disclosure: {member} {asset}".strip()
    return f"{source} signal for {ticker or 'unknown ticker'}".strip()


def normalize_edgar_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize an SEC EDGAR Form 4 payload into the shared contract.

    ``paper_edgar_form_4_ingestion`` writes payloads with keys cik,
    accession_number, filing_date, report_date, primary_document,
    primary_doc_description, items, document_url, document_text, and
    ticker.  The normalized signal carries the ticker from the payload.
    """
    return {
        "source": "sec_edgar",
        "ticker": _extract_ticker(payload, "sec_edgar"),
        "signal_type": "form4_purchase",
        "direction": "long",
        "rationale": _rationale_for("sec_edgar", payload, ""),
        "published_ts": payload.get("filing_date") or "",
    }


def normalize_congressional_raw_dict(raw: dict[str, Any]) -> dict[str, Any]:
    """Normalize a raw congressional disclosure dict into the contract.

    ``paper_congressional_trade_ingestion`` writes the raw record dict
    as payload.  The normalized signal carries the ticker from
    ``asset_ticker``/``ticker``, the signal_type ``congress_trade``,
    and a direction derived from the transaction type.
    """
    ticker = _extract_ticker(raw, "congress")
    return {
        "source": "congress",
        "ticker": ticker,
        "signal_type": "congress_trade",
        "direction": _direction_for("congress", raw),
        "rationale": _rationale_for("congress", raw, ticker),
        "published_ts": raw.get("published_ts")
        or raw.get("published_at")
        or raw.get("filing_date")
        or "",
    }


def _normalize_payload(payload: dict[str, Any], source: str) -> dict[str, Any]:
    """Normalize an arbitrary payload into the shared signal contract."""
    ticker = _extract_ticker(payload, source)
    return {
        "source": source,
        "ticker": ticker,
        "signal_type": _signal_type_for(source),
        "direction": _direction_for(source, payload),
        "rationale": _rationale_for(source, payload, ticker),
        "published_ts": payload.get("published_ts")
        or payload.get("published_at")
        or payload.get("filing_date")
        or "",
    }


def _normalize_item(item: dict[str, Any]) -> dict[str, Any]:
    """Normalize one ingested_item row into the shared signal contract."""
    source = _source_of(item)
    payload = item.get("payload") or {}

    if source == "sec_edgar":
        return normalize_edgar_payload(payload)
    if source == "congress":
        return normalize_congressional_raw_dict(payload)

    normalized = _normalize_payload(payload, source)
    # Preserve the row's published_ts when the payload lacks one.
    if not normalized["published_ts"]:
        normalized["published_ts"] = item.get("published_ts") or ""
    return normalized


async def generate_signals(
    timestamp: datetime,
    *,
    db: Optional[Any] = None,
) -> list[dict[str, Any]]:
    """Return signals from point-in-time data, ordered by source weights.

    Every ingested_item whose ``published_ts`` is at or before
    ``timestamp`` and whose normalized ticker is non-empty is included.
    Each included signal is persisted to the ``signal`` table via
    ``paper_audit_trail_writer.write_signal`` (idempotently) and the
    returned dict carries the saved row's id as ``signal_id``.

    Items are ordered by source weight (descending): SEC EDGAR first,
    then 13F, then Congressional trades, then FINRA, then news, then
    Reddit.  Items from the same source keep their insertion order.

    Each returned dict carries the keys downstream consumers read:
    ``source``, ``ticker``, ``signal_type``, ``direction``,
    ``rationale``, ``published_ts``, and ``signal_id``.
    """
    own_db = db is None
    if own_db:
        db = async_session()
    try:
        # Fetch ingested_item rows as-of timestamp, ordered by id.
        result = await db.execute(
            text(
                "SELECT id, source, payload, published_ts "
                "FROM ingested_item "
                "WHERE published_ts <= :ts "
                "ORDER BY id"
            ),
            {"ts": timestamp},
        )
        rows = result.mappings().all()
        items = [dict(row) for row in rows]

        # End the read transaction before writing signals via a separate
        # session (write_signal opens its own session).
        await db.commit()

        weighted: list[tuple[int, int, dict[str, Any]]] = []
        for index, item in enumerate(items):
            source = _source_of(item)
            weight = SOURCE_WEIGHTS.get(source, 0)
            normalized = _normalize_item(item)

            # The signal table requires a non-empty ticker.  Items whose
            # normalized ticker is empty (e.g., SEC EDGAR Form 4 payloads
            # without a ticker key) cannot be saved and are skipped; they
            # are not returned.
            if not normalized["ticker"]:
                logger.debug(
                    "Skipping signal for ingested_item %s: empty ticker",
                    item.get("id"),
                )
                continue

            ingested_item_id = item["id"]

            # Idempotent save: look up an existing signal row for this
            # ingested_item_id and reuse its id; otherwise write a new row.
            existing = await db.execute(
                text("SELECT id FROM signal WHERE ingested_item_id = :iid"),
                {"iid": ingested_item_id},
            )
            existing_id = existing.scalar()
            if existing_id is not None:
                signal_id = existing_id
            else:
                signal_id = await write_signal(
                    ingested_item_id=ingested_item_id,
                    ticker=normalized["ticker"],
                    signal_type=normalized["signal_type"],
                    direction=normalized["direction"],
                    rationale=normalized["rationale"],
                )

            normalized["signal_id"] = signal_id
            weighted.append((weight, index, normalized))

        weighted.sort(key=lambda entry: (-entry[0], entry[1]))
        return [entry[2] for entry in weighted]
    finally:
        if own_db:
            await db.close()
