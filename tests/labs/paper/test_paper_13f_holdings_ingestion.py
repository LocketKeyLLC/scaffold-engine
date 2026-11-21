# tests/test_paper_13f_holdings_ingestion.py
"""Tests for the 13F information-table lookup.

EDGAR's submissions JSON reports the cover page with a folder prefix
(``xslForm13F_X01/primary_doc.xml``) while index.json lists the bare name
(``primary_doc.xml``). The lookup must compare by the last path segment,
never return the cover page, and pick the information table whatever its
name and position in the listing.
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules import paper_13f_holdings_ingestion as mod

CIK = "0001067983"

TABLE_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<informationTable xmlns="http://www.sec.gov/edgar/document/thirteenf/informationtable">
  <infoTable>
    <nameOfIssuer>APPLE INC</nameOfIssuer>
    <titleOfClass>COM</titleOfClass>
    <cusip>037833100</cusip>
    <value>12345</value>
    <shrsOrPrnAmt>
      <sshPrnamt>100</sshPrnamt>
      <sshPrnamtType>SH</sshPrnamtType>
    </shrsOrPrnAmt>
    <investmentDiscretion>SOLE</investmentDiscretion>
    <votingAuthority>
      <Sole>100</Sole>
      <Shared>0</Shared>
      <None>0</None>
    </votingAuthority>
  </infoTable>
</informationTable>
"""

COVER_XML = b"""<?xml version="1.0" encoding="UTF-8"?>
<edgarSubmission xmlns="http://www.sec.gov/edgar/secedgar/13f/coverpage">
  <headerData><submissionType>13F-HR</submissionType></headerData>
</edgarSubmission>
"""

SUMMARY_XML = b"""<?xml version="1.0"?><FilingSummary><MyReports/></FilingSummary>"""


class FakeResponse:
    """Minimal httpx.Response stand-in."""

    def __init__(
        self, *, json_data: object = None, content: bytes = b"", status_code: int = 200
    ) -> None:
        self._json_data = json_data
        self.content = content
        self.status_code = status_code
        self.text = content.decode("utf-8", "replace")

    def json(self):
        if self._json_data is None:
            raise ValueError("response has no JSON body")
        return self._json_data

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """httpx.AsyncClient stand-in that serves prepared URLs and records calls."""

    def __init__(self, responses: dict[str, FakeResponse]) -> None:
        self._responses = responses
        self.calls: list[str] = []

    async def get(self, url: str, *, headers=None, params=None):
        self.calls.append(url)
        if url not in self._responses:
            raise AssertionError(f"unexpected GET {url}")
        return self._responses[url]


def _index(*names: str) -> dict:
    return {
        "directory": {
            "item": [
                {"name": name, "type": "xml" if name.endswith(".xml") else "text.gif"}
                for name in names
            ]
        }
    }


def _accession() -> str:
    return f"0001067983-24-{uuid.uuid4().int % 10**8:08d}"


async def _lookup(accession: str, primary_document: str, index: dict, extra=None):
    index_url = mod._build_index_url(CIK, accession)
    responses = {index_url: FakeResponse(json_data=index)}
    responses.update(extra or {})
    client = FakeClient(responses)
    found = await mod._fetch_information_table_file(
        CIK, accession, primary_document, client, {"User-Agent": "test"}
    )
    return found, client


@pytest.mark.asyncio
async def test_lookup_picks_table_listed_after_cover_page(monkeypatch):
    monkeypatch.setattr(mod, "RATE_LIMIT_SECONDS", 0.0)
    accession = _accession()
    found, _client = await _lookup(
        accession,
        "xslForm13F_X01/primary_doc.xml",
        _index("primary_doc.xml", "0001067983-24-000001-index.htm", "60471.xml"),
    )
    assert found == "60471.xml"


@pytest.mark.asyncio
async def test_lookup_picks_table_listed_before_cover_page(monkeypatch):
    monkeypatch.setattr(mod, "RATE_LIMIT_SECONDS", 0.0)
    accession = _accession()
    found, _client = await _lookup(
        accession,
        "xslForm13F_X02/primary_doc.xml",
        _index("60471.xml", "primary_doc.xml", "0001067983-24-000001-index.htm"),
    )
    assert found == "60471.xml"


@pytest.mark.asyncio
async def test_lookup_returns_none_when_only_the_cover_page_is_xml(monkeypatch):
    monkeypatch.setattr(mod, "RATE_LIMIT_SECONDS", 0.0)
    accession = _accession()
    found, client = await _lookup(
        accession,
        "xslForm13F_X01/primary_doc.xml",
        _index("primary_doc.xml", "0001067983-24-000001-index.htm"),
    )
    assert found is None
    # Only index.json was fetched; the cover page was never downloaded.
    assert client.calls == [mod._build_index_url(CIK, accession)]


@pytest.mark.asyncio
async def test_lookup_uses_root_element_when_several_xml_candidates(monkeypatch):
    monkeypatch.setattr(mod, "RATE_LIMIT_SECONDS", 0.0)
    accession = _accession()
    summary_url = mod._build_archive_url(CIK, accession, "FilingSummary.xml")
    table_url = mod._build_archive_url(CIK, accession, "60471.xml")
    found, client = await _lookup(
        accession,
        "xslForm13F_X01/primary_doc.xml",
        _index("primary_doc.xml", "FilingSummary.xml", "60471.xml"),
        extra={
            summary_url: FakeResponse(content=SUMMARY_XML),
            table_url: FakeResponse(content=TABLE_XML),
        },
    )
    assert found == "60471.xml"
    assert summary_url in client.calls
    assert client.calls.index(summary_url) < client.calls.index(table_url)


@pytest.mark.asyncio
async def test_lookup_skips_a_prefixed_cover_page_in_the_index(monkeypatch):
    monkeypatch.setattr(mod, "RATE_LIMIT_SECONDS", 0.0)
    accession = _accession()
    found, _client = await _lookup(
        accession,
        "xslForm13F_X01/primary_doc.xml",
        _index("xslForm13F_X01/primary_doc.xml", "60471.xml"),
    )
    assert found == "60471.xml"


@pytest.mark.asyncio
async def test_ingest_picks_table_and_writes_one_row_per_holding(monkeypatch):
    monkeypatch.setattr(mod, "RATE_LIMIT_SECONDS", 0.0)
    accession = _accession()
    primary = "xslForm13F_X02/primary_doc.xml"

    submissions_url = mod.SUBMISSIONS_URL_TEMPLATE.format(CIK)
    index_url = mod._build_index_url(CIK, accession)
    table_url = mod._build_archive_url(CIK, accession, "60471.xml")

    submissions = {
        "filings": {
            "recent": [
                {
                    "form": "13F-HR",
                    "accessionNumber": accession,
                    "filingDate": "2024-02-14",
                    "primaryDocument": primary,
                }
            ],
            "files": [],
        }
    }
    client = FakeClient(
        {
            submissions_url: FakeResponse(json_data=submissions),
            index_url: FakeResponse(
                json_data=_index("primary_doc.xml", "60471.xml")
            ),
            table_url: FakeResponse(content=TABLE_XML),
        }
    )

    async def _fake_values_for(db, names):
        return {"SEC_CONTACT_EMAIL": "research@scaffold.invalid"}

    async with async_session() as db:
        with patch(
            "app.modules.paper_13f_holdings_ingestion.values_for", _fake_values_for
        ):
            written = await mod.ingest_13f_holdings(CIK, db=db, client=client)

    assert written == 1

    external_id = f"{accession}:037833100"
    async with async_session() as db:
        result = await db.execute(
            text(
                "SELECT source, external_id, payload FROM ingested_item "
                "WHERE external_id = :external_id"
            ),
            {"external_id": external_id},
        )
        row = result.fetchone()

    assert row is not None
    assert row[0] == "sec_edgar"
    assert row[1] == external_id
    payload = row[2]
    if isinstance(payload, str):
        payload = json.loads(payload)
    assert payload["cusip"] == "037833100"
    assert payload["accessionNumber"] == accession
    assert payload["nameOfIssuer"] == "APPLE INC"
