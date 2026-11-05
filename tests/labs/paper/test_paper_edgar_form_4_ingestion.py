# tests/test_paper_edgar_form_4_ingestion.py
"""Tests for app.modules.paper_edgar_form_4_ingestion."""
from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.database import async_session
from app.modules import paper_edgar_form_4_ingestion as mod

pytestmark = pytest.mark.asyncio


class FakeResponse:
    """A minimal fake httpx response for the module's client.get calls."""

    def __init__(self, json_data=None, content=b"", status_code=200, text=""):
        self._json_data = json_data
        self.content = content
        self.status_code = status_code
        self.text = text

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://fake")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}", request=request, response=response
            )


def _submissions_url(cik: int) -> str:
    """Return the SEC submissions JSON URL for a CIK."""
    return f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json"


def _archive_url(cik: int | str, accession: str, primary_doc: str) -> str:
    """Return the SEC archive URL for a filing's primary document."""
    return f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession.replace('-', '')}/{primary_doc}"


def _form4_filing(
    *,
    form: str = "4",
    accession: str = "0001-24-000001",
    filing_date: str = "2024-01-02",
    report_date: str = "2024-01-01",
    primary_doc: str = "primary.xml",
    primary_desc: str = "FORM 4",
    items: str = "4",
) -> dict:
    """Return a single filing row dict."""
    return {
        "form": form,
        "accessionNumber": accession,
        "filingDate": filing_date,
        "reportDate": report_date,
        "primaryDocument": primary_doc,
        "primaryDocDescription": primary_desc,
        "items": items,
    }


def _form4_xml(
    *,
    ticker: str = "AAPL",
    transaction_code: str = "P",
    shares: str = "100",
    price: str = "150.0",
) -> bytes:
    """Return a minimal Form 4 XML document."""
    xml = f"""<ownershipDocument>
      <issuer>
        <issuerTradingSymbol>{ticker}</issuerTradingSymbol>
      </issuer>
      <nonDerivativeTable>
        <nonDerivativeTransaction>
          <transactionCode>{transaction_code}</transactionCode>
          <transactionShares><value>{shares}</value></transactionShares>
          <transactionPricePerShare><value>{price}</value></transactionPricePerShare>
        </nonDerivativeTransaction>
      </nonDerivativeTable>
    </ownershipDocument>"""
    return xml.encode("utf-8")


def _submissions_json(
    *,
    cik: int = 123,
    tickers: list[str] | None = None,
    forms: list[str] | None = None,
    accessions: list[str] | None = None,
    filing_dates: list[str] | None = None,
    report_dates: list[str] | None = None,
    primary_docs: list[str] | None = None,
    primary_descs: list[str] | None = None,
    items: list[str] | None = None,
    files: list[dict] | None = None,
) -> dict:
    """Return a SEC submissions JSON dict."""
    n = len(forms or [])
    return {
        "cik": str(cik),
        "name": "TEST ISSUER",
        "tickers": tickers or [],
        "filings": {
            "recent": {
                "form": forms or [],
                "accessionNumber": accessions or [],
                "filingDate": filing_dates or [],
                "reportDate": report_dates or [],
                "primaryDocument": primary_docs or [],
                "primaryDocDescription": primary_descs or [],
                "items": items or [],
            },
            "files": files or [],
        },
    }


async def _count_ingested(db, source: str = "sec_edgar") -> int:
    """Return the number of ingested_item rows for a source."""
    result = await db.execute(
        text("SELECT count(*) FROM ingested_item WHERE source = :s"), {"s": source}
    )
    return result.scalar_one()


def _make_client(responses: dict) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient backed by a mock transport from a URL->response map."""

    async def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        resp = responses.get(url)
        if resp is None:
            return httpx.Response(404, text="not found", request=request)
        if isinstance(resp, FakeResponse):
            if resp._json_data is not None:
                return httpx.Response(resp.status_code, json=resp._json_data, request=request)
            return httpx.Response(
                resp.status_code, content=resp.content, text=resp.text, request=request
            )
        if isinstance(resp, dict):
            return httpx.Response(200, json=resp, request=request)
        return httpx.Response(200, content=resp, request=request)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@contextmanager
def patch_values_for(return_value: dict):
    """Patch paper_edgar_form_4_ingestion.values_for to return return_value."""
    with patch(
        "app.modules.paper_edgar_form_4_ingestion.values_for",
        new=AsyncMock(return_value=return_value),
    ):
        yield


@pytest_asyncio.fixture
async def _clean_edgar_tables():
    """The sandbox empties this job's tables before every test; no cleanup needed."""
    yield


async def test_ingest_form_4_filings_pagination_fetches_additional_files(
    _clean_edgar_tables, monkeypatch
):
    """Ingestion follows the submissions JSON pagination files."""
    monkeypatch.setattr(mod, "SEC_MIN_REQUEST_INTERVAL_SECONDS", 0)
    cik = 123
    first = _submissions_json(
        cik=cik,
        tickers=["AAPL"],
        forms=["4"],
        accessions=["0001-24-000001"],
        filing_dates=["2024-01-02"],
        primary_docs=["doc1.xml"],
        files=[{"name": "extra.json", "url": "https://data.sec.gov/submissions/extra.json"}],
    )
    second = _submissions_json(
        cik=cik,
        tickers=[],
        forms=["4"],
        accessions=["0002-24-000002"],
        filing_dates=["2024-01-03"],
        primary_docs=["doc2.xml"],
    )

    responses = {
        _submissions_url(cik): FakeResponse(json_data=first),
        "https://data.sec.gov/submissions/extra.json": FakeResponse(json_data=second),
        _archive_url(cik, "0001-24-000001", "doc1.xml"): FakeResponse(content=_form4_xml()),
        _archive_url(cik, "0002-24-000002", "doc2.xml"): FakeResponse(content=_form4_xml()),
    }

    client = _make_client(responses)
    async with async_session() as db:
        with patch(
            "app.modules.paper_edgar_form_4_ingestion.values_for",
            new=AsyncMock(return_value={"SEC_CONTACT_EMAIL": "test@example.com"}),
        ):
            count = await mod.ingest_form_4_filings(
                db, cik, client=client, contact_email="test@example.com", max_filings=10
            )
        assert count == 2
        assert await _count_ingested(db) == 2
    await client.aclose()


async def test_ingest_form_4_filings_builds_user_agent_without_claiming_required(
    _clean_edgar_tables,
):
    """The module no longer claims a contact e-mail User-Agent is required."""
    assert "required" not in mod.__doc__.lower()
    assert mod._build_user_agent("test@example.com") == "PaperTradingScaffold Research test@example.com"


async def test_ingest_form_4_filings_skips_failed_primary_doc_and_continues(
    _clean_edgar_tables, monkeypatch
):
    """A failed primary-document fetch skips that filing but not later ones."""
    monkeypatch.setattr(mod, "SEC_MIN_REQUEST_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(
        "app.modules.paper_edgar_form_4_ingestion.values_for",
        AsyncMock(return_value={"SEC_CONTACT_EMAIL": "test@example.com"}),
    )
    cik = 123
    submissions = _submissions_json(
        cik=cik,
        tickers=["AAPL"],
        forms=["4", "4"],
        accessions=["0001-24-000001", "0002-24-000002"],
        filing_dates=["2024-01-02", "2024-01-03"],
        primary_docs=["doc1.xml", "doc2.xml"],
    )
    responses = {
        _submissions_url(cik): FakeResponse(json_data=submissions),
        _archive_url(cik, "0001-24-000001", "doc1.xml"): FakeResponse(
            status_code=500, text="boom"
        ),
        _archive_url(cik, "0002-24-000002", "doc2.xml"): FakeResponse(content=_form4_xml()),
    }
    client = _make_client(responses)
    async with async_session() as db:
        count = await mod.ingest_form_4_filings(
            db, cik, client=client, contact_email="test@example.com", max_filings=10
        )
        assert count == 1
        assert await _count_ingested(db) == 1
    await client.aclose()
