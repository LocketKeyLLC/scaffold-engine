# app/modules/paper_13f_holdings_ingestion.py
"""SEC EDGAR 13F holdings ingestion for the scaffold engine.

Fetches SEC submissions JSON for a CIK, finds 13F-HR and 13F-HR/A
filings, downloads the primary XML document, parses each holding, and
writes one ``ingested_item`` row per holding with ``source='sec_edgar'``.

Polite rate limiting: SEC's fair-access guidance is 10 requests per
second; we stay well under that with a 0.5s minimum inter-request delay.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from typing import Any, Iterable
import xml.etree.ElementTree as ET

from sqlalchemy import text

from app.modules.runner_secrets import values_for

logger = logging.getLogger("scaffold")

# Fixed values that must never change at runtime (module constants).
SUBMISSIONS_URL_TEMPLATE = "https://data.sec.gov/submissions/CIK{}.json"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data"
RATE_LIMIT_SECONDS = 0.5  # well under SEC's 10 req/s limit
SEC_USER_AGENT_SECRET = "SEC_CONTACT_EMAIL"
SEC_USER_AGENT_TEMPLATE = "PaperTradingScaffold Research {email}"
FORM_13F_TYPES = frozenset({"13F-HR", "13F-HR/A"})
XML_NAMESPACE = "http://www.sec.gov/edgar/document/thirteenf/informationtable"


def _parse_edgar_date(value: str | None) -> datetime | None:
    """Parse an EDGAR date string (``YYYY-MM-DD`` or ISO-8601) into a tz-aware datetime."""
    if not value:
        return None
    try:
        if "T" in value:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        logger.warning("Could not parse EDGAR date: %r", value)
        return None


def _iter_recent_rows(payload: Any) -> Iterable[dict[str, Any]]:
    """Yield recent filing rows from either ``filings.recent`` or a bare ``recent``."""
    if not isinstance(payload, dict):
        return
    filings = payload.get("filings")
    if isinstance(filings, dict):
        recent = filings.get("recent")
        if isinstance(recent, list):
            yield from recent
    recent = payload.get("recent")
    if isinstance(recent, list):
        yield from recent


def _find_13f_hr_rows(payload: Any) -> list[dict[str, Any]]:
    """Return 13F-HR / 13F-HR/A rows that have the fields we need."""
    rows: list[dict[str, Any]] = []
    for entry in _iter_recent_rows(payload):
        if not isinstance(entry, dict):
            continue
        form = entry.get("form")
        if form not in FORM_13F_TYPES:
            continue
        accession = entry.get("accessionNumber")
        filing_date = entry.get("filingDate")
        primary = entry.get("primaryDocument")
        if not accession or not filing_date or not primary:
            continue
        rows.append(
            {
                "form": form,
                "accessionNumber": accession,
                "filingDate": filing_date,
                "primaryDocument": primary,
            }
        )
    return rows


def _build_archive_url(cik: str, accession: str, primary_document: str) -> str:
    """Build the per-filing archive URL."""
    accession_no_dashes = accession.replace("-", "")
    return f"{ARCHIVES_BASE}/{cik}/{accession_no_dashes}/{primary_document}"


async def _rate_limited_request(
    client: Any, url: str, *, headers: dict[str, str] | None = None
) -> Any:
    """Sleep RATE_LIMIT_SECONDS, then GET ``url`` and raise on HTTP errors."""
    await asyncio.sleep(RATE_LIMIT_SECONDS)
    response = await client.get(url, headers=headers)
    response.raise_for_status()
    return response


async def _fetch_submissions(
    cik: str, client: Any, headers: dict[str, str]
) -> dict[str, Any]:
    """Fetch the base submissions JSON for a CIK."""
    url = SUBMISSIONS_URL_TEMPLATE.format(cik)
    response = await _rate_limited_request(client, url, headers=headers)
    data = response.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Unexpected submissions payload for CIK {cik}: {type(data)!r}")
    return data


async def _fetch_additional_rows(
    cik: str,
    client: Any,
    headers: dict[str, str],
    initial_payload: dict[str, Any],
) -> list[dict[str, Any]]:
    """Collect 13F rows from the base payload and any additional files array."""
    rows = _find_13f_hr_rows(initial_payload)
    seen = {(r["accessionNumber"], r["primaryDocument"]) for r in rows}

    filings = initial_payload.get("filings", {})
    files = filings.get("files", []) if isinstance(filings, dict) else []
    if not isinstance(files, list):
        return rows

    for file_info in files:
        if not isinstance(file_info, dict):
            continue
        url = file_info.get("url") or file_info.get("name")
        if not url:
            continue
        response = await _rate_limited_request(client, url, headers=headers)
        data = response.json()
        for row in _find_13f_hr_rows(data):
            key = (row["accessionNumber"], row["primaryDocument"])
            if key not in seen:
                rows.append(row)
                seen.add(key)
    return rows


def _voting_text(voting_el: ET.Element, tag: str) -> str | None:
    el = voting_el.find(tag, {"t": XML_NAMESPACE})
    if el is not None and el.text:
        return el.text.strip()
    return None


def _parse_13f_xml(content: bytes | str) -> list[dict[str, Any]]:
    """Parse post-2021 13F XML into a list of holding dicts."""
    if isinstance(content, str):
        content = content.encode("utf-8")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        logger.warning("Malformed 13F XML: %s", exc)
        return []

    ns = {"t": XML_NAMESPACE}
    holdings: list[dict[str, Any]] = []
    for info in root.findall(".//t:infoTable", ns):
        def txt(path: str) -> str | None:
            el = info.find(path, ns)
            if el is not None and el.text:
                return el.text.strip()
            return None

        name = txt("t:nameOfIssuer")
        title = txt("t:titleOfClass")
        cusip = txt("t:cusip")
        value = txt("t:value")
        shares = None
        shrs_or_prn = info.find("t:shrsOrPrnAmt", ns)
        if shrs_or_prn is not None:
            ssh = shrs_or_prn.find("t:sshPrnamt", ns)
            if ssh is not None and ssh.text:
                shares = ssh.text.strip()
        ssh_type = txt("t:sshPrnamtType")
        put_call = txt("t:putCall")
        discretion = txt("t:investmentDiscretion")
        voting = info.find("t:votingAuthority", ns)
        sole = shared = none = None
        if voting is not None:
            sole = _voting_text(voting, "t:Sole")
            shared = _voting_text(voting, "t:Shared")
            none = _voting_text(voting, "t:None")

        if not name and not cusip:
            continue
        holdings.append(
            {
                "nameOfIssuer": name,
                "titleOfClass": title,
                "cusip": cusip,
                "value": value,
                "shares": shares,
                "sshPrnamtType": ssh_type,
                "putCall": put_call,
                "investmentDiscretion": discretion,
                "votingAuthority": {
                    "Sole": sole,
                    "Shared": shared,
                    "None": none,
                },
                "ticker": None,
            }
        )
    return holdings


async def _insert_holding(
    db: Any,
    *,
    source: str,
    external_id: str,
    published_ts: datetime,
    payload: dict[str, Any],
) -> bool:
    """Insert one ingested_item row, returning True if it was newly inserted."""
    result = await db.execute(
        text(
            """
            INSERT INTO ingested_item (source, external_id, published_ts, payload)
            VALUES (:source, :external_id, :published_ts, CAST(:payload AS json))
            ON CONFLICT (source, external_id) DO NOTHING
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
    return row is not None


async def ingest_13f_holdings(cik: str, *, db, client) -> int:
    """Fetch and record 13F holdings for one CIK.

    Returns the number of new ``ingested_item`` rows written.
    """
    secrets = await values_for(db, [SEC_USER_AGENT_SECRET])
    contact_email = secrets.get(SEC_USER_AGENT_SECRET)
    if not contact_email:
        raise RuntimeError(
            f"Missing {SEC_USER_AGENT_SECRET} secret. "
            "Store it on the Machines page -> Secrets."
        )

    headers = {"User-Agent": SEC_USER_AGENT_TEMPLATE.format(email=contact_email)}
    initial = await _fetch_submissions(cik, client, headers)
    rows = await _fetch_additional_rows(cik, client, headers, initial)

    written = 0
    for row in rows:
        url = _build_archive_url(cik, row["accessionNumber"], row["primaryDocument"])
        response = await _rate_limited_request(client, url, headers=headers)
        content = response.content
        holdings = _parse_13f_xml(content)
        if not holdings:
            continue

        published_ts = _parse_edgar_date(row["filingDate"])
        if published_ts is None:
            continue

        for holding in holdings:
            external_id = (
                f"{row['accessionNumber']}:"
                f"{holding.get('cusip') or holding.get('nameOfIssuer') or 'unknown'}"
            )
            payload = {
                "accessionNumber": row["accessionNumber"],
                "form": row["form"],
                "filingDate": row["filingDate"],
                "primaryDocument": row["primaryDocument"],
                **holding,
            }
            inserted = await _insert_holding(
                db,
                source="sec_edgar",
                external_id=external_id,
                published_ts=published_ts,
                payload=payload,
            )
            if inserted:
                written += 1

    await db.commit()
    return written
