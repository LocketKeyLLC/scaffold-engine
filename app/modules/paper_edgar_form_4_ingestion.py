# app/modules/paper_edgar_form_4_ingestion.py
"""SEC EDGAR Form 4 ingestion for the paper trading scaffold.

Fetches SEC submissions JSON for Form 4 filings and their primary
documents, records them in the audit-trail ``ingested_item`` table, and
writes ``form4_purchase`` signals when a Form 4 reports a purchase.

SEC publishes a fair-access request rate of 10 requests/second. This
module uses a conservative 0.5s minimum inter-request delay and a
descriptive User-Agent that includes the operator's contact e-mail. The
SEC API documentation does not require a specific User-Agent string;
the contact e-mail is included solely as a courtesy.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

from sqlalchemy import text

from app.database import async_session
from app.modules.runner_secrets import values_for

logger = logging.getLogger("scaffold")

# Fixed values that must never change at runtime (module constants).
SEC_SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SEC_EDGAR_ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"
SEC_USER_AGENT_TEMPLATE = "PaperTradingScaffold Research {email}"
SEC_MIN_REQUEST_INTERVAL_SECONDS = 0.5
SEC_USER_AGENT_SECRET = "SEC_CONTACT_EMAIL"
FORM_4_SUBMISSION_TYPE = "4"


def _parse_edgar_date(value: str | None) -> datetime | None:
    """Parse an EDGAR date string into a timezone-aware UTC datetime."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
            try:
                dt = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
        else:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _build_user_agent(contact_email: str) -> str:
    """Build a descriptive User-Agent with the operator's contact e-mail."""
    return SEC_USER_AGENT_TEMPLATE.format(email=contact_email)


def _build_filing_url(cik: int | str, accession_number: str, primary_document: str) -> str:
    """Build the archive URL for a single filing's primary document."""
    cik_int = int(cik)
    accession_no_dashes = accession_number.replace("-", "")
    return f"{SEC_EDGAR_ARCHIVE_BASE}/{cik_int}/{accession_no_dashes}/{primary_document}"


def _ticker_from_submissions(submissions: dict) -> str | None:
    """Return the first ticker from a submissions JSON, or None."""
    tickers = submissions.get("tickers") or []
    if tickers:
        return str(tickers[0])
    return None


def _is_form4(form: str | None) -> bool:
    """Return True when a form is Form 4 or a Form 4 amendment."""
    if not form:
        return False
    return form == FORM_4_SUBMISSION_TYPE or form.startswith("4/")


def _extract_form_4_filings(submissions: dict) -> list[dict]:
    """Extract Form 4 filing rows from a submissions JSON's recent section."""
    filings = submissions.get("filings", {})
    recent = filings.get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    primary_docs = recent.get("primaryDocument", [])
    primary_descs = recent.get("primaryDocDescription", [])
    items = recent.get("items", [])

    result: list[dict] = []
    for idx, form in enumerate(forms):
        if not _is_form4(form):
            continue
        result.append(
            {
                "form": form,
                "accessionNumber": accessions[idx] if idx < len(accessions) else None,
                "filingDate": filing_dates[idx] if idx < len(filing_dates) else None,
                "reportDate": report_dates[idx] if idx < len(report_dates) else None,
                "primaryDocument": primary_docs[idx] if idx < len(primary_docs) else None,
                "primaryDocDescription": primary_descs[idx] if idx < len(primary_descs) else None,
                "items": items[idx] if idx < len(items) else None,
            }
        )
    return result


async def _rate_limited_get_json(client, url: str, headers: dict) -> dict:
    """GET a JSON document with the module's polite rate limiting."""
    await asyncio.sleep(SEC_MIN_REQUEST_INTERVAL_SECONDS)
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    return response.json()


async def _rate_limited_get_bytes(client, url: str, headers: dict) -> bytes:
    """GET a binary document with the module's polite rate limiting."""
    await asyncio.sleep(SEC_MIN_REQUEST_INTERVAL_SECONDS)
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    return response.content


async def _fetch_all_form4_filings(
    client, cik: int, headers: dict, max_filings: int
) -> tuple[list[dict], dict]:
    """Fetch Form 4 filings from the initial submissions JSON and pagination files."""
    url = SEC_SUBMISSIONS_URL_TEMPLATE.format(cik=int(cik))
    data = await _rate_limited_get_json(client, url, headers)
    filings = _extract_form_4_filings(data)

    files = data.get("filings", {}).get("files", [])
    for file_info in files:
        if len(filings) >= max_filings:
            break
        file_url = file_info.get("url") or file_info.get("name")
        if not file_url or not str(file_url).startswith("http"):
            continue
        more_data = await _rate_limited_get_json(client, str(file_url), headers)
        filings.extend(_extract_form_4_filings(more_data))

    return filings[:max_filings], data


async def _resolve_contact_email(db, contact_email: str | None) -> str:
    """Return the provided contact e-mail, or read it from the secret store."""
    if contact_email:
        return contact_email
    secrets = await values_for(db, [SEC_USER_AGENT_SECRET])
    email = secrets.get(SEC_USER_AGENT_SECRET)
    if not email:
        raise RuntimeError(
            "Missing SEC_CONTACT_EMAIL secret; store it on the Machines page -> Secrets."
        )
    return email


def _local_name(tag: str) -> str:
    """Return the local part of an XML tag, ignoring any namespace."""
    return tag.rsplit("}", 1)[-1]


def _element_text(elem: ET.Element) -> str | None:
    """Return the text of an element, including a nested <value> child."""
    text = (elem.text or "").strip()
    if text:
        return text
    for child in elem:
        if _local_name(child.tag) == "value":
            return (child.text or "").strip()
    return None


def _parse_form4_xml(xml_bytes: bytes | str) -> dict:
    """Parse a Form 4 XML document and return transaction metadata."""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        logger.warning("Could not parse Form 4 XML")
        return {}

    ticker = None
    for elem in root.iter():
        if _local_name(elem.tag) == "issuerTradingSymbol":
            ticker = _element_text(elem)
            if ticker:
                break

    is_purchase = False
    shares = None
    price = None
    transaction_code = None

    for elem in root.iter():
        local = _local_name(elem.tag)
        if local in ("nonDerivativeTransaction", "derivativeTransaction"):
            code = None
            shares_val = None
            price_val = None
            for child in elem.iter():
                child_local = _local_name(child.tag)
                if child_local == "transactionCode":
                    code = _element_text(child)
                elif child_local == "transactionShares":
                    shares_val = _element_text(child)
                elif child_local == "transactionPricePerShare":
                    price_val = _element_text(child)
            if code == "P":
                is_purchase = True
                transaction_code = code
                shares = shares_val
                price = price_val
                break

    return {
        "ticker": ticker,
        "is_purchase": is_purchase,
        "shares": shares,
        "price": price,
        "transaction_code": transaction_code,
    }


async def _insert_ingested_item(
    db, *, source: str, external_id: str, published_ts: datetime, payload: dict
) -> int:
    """Insert an ingested_item row and return its id."""
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
    return result.scalar_one()


async def _insert_signal(
    db,
    *,
    ingested_item_id: int,
    ticker: str,
    signal_type: str,
    direction: str,
    rationale: str,
) -> int:
    """Insert a signal row and return its id."""
    result = await db.execute(
        text(
            """
            INSERT INTO signal (ingested_item_id, ticker, signal_type, direction, rationale)
            VALUES (:ingested_item_id, :ticker, :signal_type, :direction, :rationale)
            RETURNING id
            """
        ),
        {
            "ingested_item_id": ingested_item_id,
            "ticker": ticker,
            "signal_type": signal_type,
            "direction": direction,
            "rationale": rationale,
        },
    )
    return result.scalar_one()


async def ingest_form_4_filings(
    db,
    cik: int,
    *,
    client,
    contact_email: str | None = None,
    max_filings: int = 50,
) -> int:
    """Fetch and record Form 4 filings for one CIK.

    Returns the number of new ``ingested_item`` rows written.
    """
    email = await _resolve_contact_email(db, contact_email)
    headers = {"User-Agent": _build_user_agent(email)}

    filings, submissions_data = await _fetch_all_form4_filings(
        client, cik, headers, max_filings
    )
    submissions_ticker = _ticker_from_submissions(submissions_data)

    processed = 0
    for filing in filings:
        accession = filing.get("accessionNumber")
        if not accession:
            continue

        existing = await db.execute(
            text(
                "SELECT id FROM ingested_item "
                "WHERE source = 'sec_edgar' AND external_id = :e"
            ),
            {"e": accession},
        )
        if existing.fetchone():
            continue

        primary_doc = filing.get("primaryDocument")
        if not primary_doc:
            continue

        url = _build_filing_url(cik, accession, primary_doc)
        try:
            xml_bytes = await _rate_limited_get_bytes(client, url, headers)
        except Exception:
            logger.warning("Failed to fetch Form 4 primary document %s", url)
            continue

        parsed = _parse_form4_xml(xml_bytes)
        ticker = parsed.get("ticker") or submissions_ticker or str(cik)
        published = _parse_edgar_date(filing.get("filingDate")) or datetime.now(timezone.utc)

        payload = {
            "form": filing.get("form"),
            "accessionNumber": accession,
            "filingDate": filing.get("filingDate"),
            "reportDate": filing.get("reportDate"),
            "primaryDocument": primary_doc,
            "primaryDocDescription": filing.get("primaryDocDescription"),
            "items": filing.get("items"),
            "ticker": ticker,
            "transaction": parsed,
        }

        ingested_id = await _insert_ingested_item(
            db,
            source="sec_edgar",
            external_id=accession,
            published_ts=published,
            payload=payload,
        )

        if parsed.get("is_purchase"):
            shares = parsed.get("shares")
            price = parsed.get("price")
            rationale = (
                f"Form 4 purchase of {shares} shares at ${price}"
                if shares and price
                else "Form 4 purchase"
            )
            await _insert_signal(
                db,
                ingested_item_id=ingested_id,
                ticker=ticker,
                signal_type="form4_purchase",
                direction="long",
                rationale=rationale,
            )

        processed += 1

    return processed


async def ingest_form_4_for_tickers(
    db,
    ticker_cik_map: dict,
    *,
    client,
    contact_email: str | None = None,
    max_filings_per_cik: int = 50,
) -> dict:
    """Ingest Form 4 filings for multiple tickers and return per-ticker counts."""
    counts = {}
    for ticker, cik in ticker_cik_map.items():
        counts[ticker] = await ingest_form_4_filings(
            db,
            int(cik),
            client=client,
            contact_email=contact_email,
            max_filings=max_filings_per_cik,
        )
    return counts
