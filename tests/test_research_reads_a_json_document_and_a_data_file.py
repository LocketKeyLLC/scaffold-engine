"""§17.1947 — the research reads a page that is not HTML: a JSON document as its field outline, a plain-text data file
by its first lines; a PDF or another binary body stays unread. Live (2026-11-19): the SEC's submissions JSON and
FINRA's daily short-sale file loaded and came back empty, so the fields three audits called UNVERIFIED stayed so."""
from __future__ import annotations

import json

from app.modules import engine_plan as ep
from app.modules import research_agent as ra

_SEC = {"cik": "0000320193", "tickers": ["AAPL"],
        "filings": {"recent": {"accessionNumber": ["0000320193-25-000079"], "form": ["4"], "filingDate": ["2025-11-03"],
                               "primaryDocument": ["xslF345X05/wk-form4.xml"]}}}


def test_a_json_document_is_its_fields():
    text, date = ra._extract_text_and_date(json.dumps(_SEC))
    assert date == "" and text.startswith("JSON document")
    assert "filings.recent.accessionNumber: list of str, e.g. '0000320193-25-000079'" in text
    assert "tickers: list of str, e.g. 'AAPL'" in text and "filings.recent.primaryDocument" in text


def test_a_pipe_delimited_file_is_its_first_lines():
    body = "Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market\n20251103|A|174645|581|473953|B,Q,N\n"
    text, _ = ra._extract_text_and_date(body)
    assert "Plain-text file, 2 lines" in text and "Date|Symbol|ShortVolume" in text


def test_html_a_pdf_and_a_binary_body_are_not():
    assert ra._non_html_text("<!doctype html><html><body>x</body></html>") is None
    assert ra._non_html_text("%PDF-1.3\n%\xe2\xe3\n3 0 obj") is None
    assert ra._non_html_text("\x00\x01\x02\x03" * 100) is None


def test_the_seed_pages_name_the_fields_the_audits_could_not():
    docs = {a["key"]: a["docs"] for a in ep._KNOWN_APIS}
    assert "https://data.sec.gov/submissions/CIK0000320193.json" in docs["SEC_EDGAR"]
    assert "https://www.reddit.com/dev/api/" in docs["REDDIT"]
    assert any(u.startswith("https://cdn.finra.org/equity/regsho/daily/") for u in docs["FINRA"])
