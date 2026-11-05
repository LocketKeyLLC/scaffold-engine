# tests/test_paper_13f_holdings_ingestion.py
"""Pytest tests for paper_13f_holdings_ingestion."""

import asyncio
import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules import paper_13f_holdings_ingestion as m

XML_ONE = b"""<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>1000000</value>
    <shrsOrPrnAmt><sshPrnamt>5000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall>N/A</putCall>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority><Sole>5000</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>
</informationTable>"""

XML_TWO = b"""<?xml version="1.0"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>MICROSOFT CORP</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>594918104</cusip>
    <value>2000000</value>
    <shrsOrPrnAmt><sshPrnamt>6000</sshPrnamt><sshPrnamtType>SH</sshPrnamtType></shrsOrPrnAmt>
    <putCall>N/A</putCall>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority><Sole>6000</Sole><Shared>0</Shared><None>0</None></votingAuthority>
  </infoTable>
</informationTable>"""


@pytest.fixture(autouse=True)
def mock_sleep():
    with patch(
        "app.modules.paper_13f_holdings_ingestion.asyncio.sleep", new_callable=AsyncMock
    ) as sleep_mock:
        yield sleep_mock


def _response(*, json_data=None, content=b"", status_code=200):
    """A plain MagicMock response: json() and raise_for_status() answer at once."""
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.content = content
    resp.raise_for_status = MagicMock(return_value=None)
    resp.status_code = status_code
    return resp


def _make_client(submissions_payload, xml_by_url=None, json_by_url=None):
    """Build a fake client whose get() is awaited and returns a MagicMock."""
    xml_by_url = xml_by_url or {}
    json_by_url = json_by_url or {}

    async def fake_get(url, *, headers=None):
        for key, val in json_by_url.items():
            if key in url:
                return _response(json_data=val)
        if "submissions" in url:
            return _response(json_data=submissions_payload)
        for key, val in xml_by_url.items():
            if key in url:
                return _response(content=val)
        return _response(content=b"", status_code=404)

    client = MagicMock()
    client.get = fake_get
    return client


def _submissions(recent_entries, files=None):
    return {"filings": {"recent": recent_entries, "files": files or []}}


def _row(
    form="13F-HR",
    accession="0001234567-25-000001",
    filing_date="2025-01-15",
    primary="primary.xml",
):
    return {
        "form": form,
        "accessionNumber": accession,
        "filingDate": filing_date,
        "primaryDocument": primary,
    }


async def _payloads(db):
    rows = (
        await db.execute(
            text("SELECT payload FROM ingested_item WHERE source='sec_edgar' ORDER BY id")
        )
    ).fetchall()
    out = []
    for r in rows:
        val = r[0]
        out.append(json.loads(val) if isinstance(val, str) else val)
    return out


@pytest.mark.asyncio
async def test_ingest_13f_single_holding_success():
    with patch(
        "app.modules.paper_13f_holdings_ingestion.values_for",
        new_callable=AsyncMock,
        return_value={"SEC_CONTACT_EMAIL": "test@example.com"},
    ):
        async with async_session() as db:
            client = _make_client(
                _submissions([_row()]),
                {"primary.xml": XML_ONE},
            )
            count = await m.ingest_13f_holdings("0001067983", db=db, client=client)
            assert count == 1

            payloads = await _payloads(db)
            assert len(payloads) == 1
            assert payloads[0]["nameOfIssuer"] == "APPLE INC"
            assert payloads[0]["ticker"] is None


@pytest.mark.asyncio
async def test_ingest_13f_follows_pagination():
    with patch(
        "app.modules.paper_13f_holdings_ingestion.values_for",
        new_callable=AsyncMock,
        return_value={"SEC_CONTACT_EMAIL": "test@example.com"},
    ):
        async with async_session() as db:
            base = _submissions(
                [_row(accession="0001", primary="p1.xml")],
                files=[{"url": "https://data.sec.gov/submissions/extra.json"}],
            )
            extra = {"recent": [_row(accession="0002", primary="p2.xml")]}
            client = _make_client(
                base,
                {"p1.xml": XML_ONE, "p2.xml": XML_TWO},
                json_by_url={"extra.json": extra},
            )
            count = await m.ingest_13f_holdings("0001067983", db=db, client=client)
            assert count == 2

            payloads = await _payloads(db)
            assert len(payloads) == 2
            names = {p["nameOfIssuer"] for p in payloads}
            assert names == {"APPLE INC", "MICROSOFT CORP"}


@pytest.mark.asyncio
async def test_ingest_skips_non_13f_forms():
    with patch(
        "app.modules.paper_13f_holdings_ingestion.values_for",
        new_callable=AsyncMock,
        return_value={"SEC_CONTACT_EMAIL": "test@example.com"},
    ):
        async with async_session() as db:
            client = _make_client(
                _submissions([_row(form="4"), _row(form="10-K")]),
                {},
            )
            count = await m.ingest_13f_holdings("0001067983", db=db, client=client)
            assert count == 0


@pytest.mark.asyncio
async def test_ingest_includes_13f_amendment():
    with patch(
        "app.modules.paper_13f_holdings_ingestion.values_for",
        new_callable=AsyncMock,
        return_value={"SEC_CONTACT_EMAIL": "test@example.com"},
    ):
        async with async_session() as db:
            client = _make_client(
                _submissions([_row(form="13F-HR/A")]),
                {"primary.xml": XML_ONE},
            )
            count = await m.ingest_13f_holdings("0001067983", db=db, client=client)
            assert count == 1


@pytest.mark.asyncio
async def test_ingest_skips_missing_required_fields():
    with patch(
        "app.modules.paper_13f_holdings_ingestion.values_for",
        new_callable=AsyncMock,
        return_value={"SEC_CONTACT_EMAIL": "test@example.com"},
    ):
        async with async_session() as db:
            bad1 = _row(primary=None)
            bad2 = _row(accession=None)
            client = _make_client(_submissions([bad1, bad2]), {})
            count = await m.ingest_13f_holdings("0001067983", db=db, client=client)
            assert count == 0


@pytest.mark.asyncio
async def test_ingest_handles_malformed_xml():
    with patch(
        "app.modules.paper_13f_holdings_ingestion.values_for",
        new_callable=AsyncMock,
        return_value={"SEC_CONTACT_EMAIL": "test@example.com"},
    ):
        async with async_session() as db:
            client = _make_client(
                _submissions([_row()]),
                {"primary.xml": b"<not-valid>"},
            )
            count = await m.ingest_13f_holdings("0001067983", db=db, client=client)
            assert count == 0


@pytest.mark.asyncio
async def test_ingest_duplicate_skipped():
    with patch(
        "app.modules.paper_13f_holdings_ingestion.values_for",
        new_callable=AsyncMock,
        return_value={"SEC_CONTACT_EMAIL": "test@example.com"},
    ):
        async with async_session() as db:
            client = _make_client(
                _submissions([_row()]),
                {"primary.xml": XML_ONE},
            )
            first = await m.ingest_13f_holdings("0001067983", db=db, client=client)
            second = await m.ingest_13f_holdings("0001067983", db=db, client=client)
            assert first == 1
            assert second == 0

            payloads = await _payloads(db)
            assert len(payloads) == 1


@pytest.mark.asyncio
async def test_ingest_without_contact_email():
    with patch(
        "app.modules.paper_13f_holdings_ingestion.values_for",
        new_callable=AsyncMock,
        return_value={},
    ):
        async with async_session() as db:
            client = _make_client(_submissions([_row()]), {})
            with pytest.raises(RuntimeError, match="SEC_CONTACT_EMAIL"):
                await m.ingest_13f_holdings("0001067983", db=db, client=client)


@pytest.mark.asyncio
async def test_rate_limit_sleep_called(mock_sleep):
    with patch(
        "app.modules.paper_13f_holdings_ingestion.values_for",
        new_callable=AsyncMock,
        return_value={"SEC_CONTACT_EMAIL": "test@example.com"},
    ):
        async with async_session() as db:
            client = _make_client(
                _submissions([_row()]),
                {"primary.xml": XML_ONE},
            )
            await m.ingest_13f_holdings("0001067983", db=db, client=client)
            assert mock_sleep.await_count >= 2
            mock_sleep.assert_awaited_with(m.RATE_LIMIT_SECONDS)
