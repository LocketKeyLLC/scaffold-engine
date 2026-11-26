"""§17.2046 — a Form 4 is read from the filing's own XML, not from the page EDGAR renders from it.

The real submissions JSON lists a Form 4's `primaryDocument` as the XSL-rendered path: Apple, accession
0000320193-25-000079, `xslF345X05/wk-form4.xml` (recorded live 2026-11-19, §17.1947). The filing folder holds the XML
itself under the bare name, as EDGAR does for 13F (`xslForm13F_X01/primary_doc.xml` listed, `primary_doc.xml` in the
folder -- §17.1975, read live). The ingester fetched the rendered path and parsed it as Form 4 XML: no
`issuerTradingSymbol`, no transactions, so every filing was recorded "not a purchase" and no `form4_purchase` signal
was ever written.

The document bodies below are not fetched (SEC refuses a fetch without a declared contact, which a test may not send):
the XML carries the Form 4 schema's own element names, the rendered page is an XHTML page.
"""
from __future__ import annotations

import logging

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules import paper_edgar_form_4_ingestion as mod

CIK = 320193
ACCESSION = "0000320193-25-000079"
FOLDER = f"https://www.sec.gov/Archives/edgar/data/{CIK}/{ACCESSION.replace('-', '')}"
SUBMISSIONS = {"cik": "0000320193", "tickers": ["AAPL"],
               "filings": {"recent": {"accessionNumber": [ACCESSION], "form": ["4"], "filingDate": ["2025-11-03"],
                                      "reportDate": ["2025-10-31"], "primaryDocument": ["xslF345X05/wk-form4.xml"],
                                      "primaryDocDescription": ["FORM 4"], "items": [""]},
                           "files": []}}
FORM_4_XML = b"""<?xml version="1.0"?>
<ownershipDocument>
  <schemaVersion>X0508</schemaVersion>
  <documentType>4</documentType>
  <issuer><issuerCik>0000320193</issuerCik><issuerName>Apple Inc.</issuerName>
    <issuerTradingSymbol>AAPL</issuerTradingSymbol></issuer>
  <nonDerivativeTable><nonDerivativeTransaction>
    <securityTitle><value>Common Stock</value></securityTitle>
    <transactionCoding><transactionFormType>4</transactionFormType><transactionCode>P</transactionCode></transactionCoding>
    <transactionAmounts><transactionShares><value>1000</value></transactionShares>
      <transactionPricePerShare><value>270.37</value></transactionPricePerShare>
      <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode></transactionAmounts>
  </nonDerivativeTransaction></nonDerivativeTable>
</ownershipDocument>
"""
RENDERED = (b'<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml"><head><title>SEC FORM 4'
            b'</title></head><body><table><tr><td>Apple Inc. [ AAPL ]</td></tr></table></body></html>')


class _Response:
    def __init__(self, url: str, body: bytes | None, json_data=None) -> None:
        self.url, self.content, self._json = url, body or b"", json_data
        self.status_code = 200 if body is not None or json_data is not None else 404

    def json(self):
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code} {self.url}")


class _Client:
    def __init__(self, documents: dict[str, bytes]) -> None:
        self.documents, self.asked = documents, []

    async def get(self, url: str, headers: dict) -> _Response:
        self.asked.append(url)
        if url == mod.SEC_SUBMISSIONS_URL_TEMPLATE.format(cik=CIK):
            return _Response(url, None, SUBMISSIONS)
        return _Response(url, self.documents.get(url))


@pytest.fixture(autouse=True)
def _no_wait(monkeypatch):
    monkeypatch.setattr(mod, "SEC_MIN_REQUEST_INTERVAL_SECONDS", 0)


async def _ingest(client: _Client) -> int:
    async with async_session() as db:
        written = await mod.ingest_form_4_filings(db, CIK, client=client, contact_email="ops@example.org",
                                                  max_filings=5)
        await db.commit()
    return written


@pytest.mark.asyncio
async def test_the_filings_own_xml_is_read_and_its_purchase_is_a_signal() -> None:
    client = _Client({f"{FOLDER}/wk-form4.xml": FORM_4_XML, f"{FOLDER}/xslF345X05/wk-form4.xml": RENDERED})
    assert await _ingest(client) == 1
    assert f"{FOLDER}/wk-form4.xml" in client.asked
    assert f"{FOLDER}/xslF345X05/wk-form4.xml" not in client.asked      # the rendering is not needed
    async with async_session() as db:
        payload = (await db.execute(text(
            "SELECT payload FROM ingested_item WHERE source = 'sec_edgar' AND external_id = :a"),
            {"a": ACCESSION})).scalar_one()
        signals = (await db.execute(text(
            "SELECT ticker, signal_type FROM signal WHERE signal_type = 'form4_purchase'"))).all()
    assert payload["transaction"]["is_purchase"] is True and payload["transaction"]["shares"] == "1000"
    assert payload["document"] == "wk-form4.xml"
    assert [tuple(s) for s in signals] == [("AAPL", "form4_purchase")]


@pytest.mark.asyncio
async def test_a_rendered_page_is_not_recorded_as_a_filing_with_no_purchase(caplog) -> None:
    client = _Client({f"{FOLDER}/xslF345X05/wk-form4.xml": RENDERED})   # the XML itself does not answer
    with caplog.at_level(logging.WARNING, logger="scaffold"):
        assert await _ingest(client) == 0
    assert client.asked[-2:] == [f"{FOLDER}/wk-form4.xml", f"{FOLDER}/xslF345X05/wk-form4.xml"]
    async with async_session() as db:
        assert (await db.execute(text("SELECT count(*) FROM ingested_item WHERE source = 'sec_edgar'"))).scalar_one() == 0
    assert "form4_document_unreadable" in caplog.text and ACCESSION in caplog.text


def test_only_an_xsl_rendering_directory_is_taken_off() -> None:
    assert mod._raw_document_name("xslF345X05/wk-form4.xml") == "wk-form4.xml"
    assert mod._raw_document_name("xslF345X04/doc4.xml") == "doc4.xml"
    assert mod._raw_document_name("wk-form4.xml") == "wk-form4.xml"
    assert mod._raw_document_name("exhibits/ex99.xml") == "exhibits/ex99.xml"
