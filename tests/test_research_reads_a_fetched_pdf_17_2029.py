"""§17.2029 — a PDF the research fetches is read as its text.

§17.1975 (2026-11-21): `sec.gov/files/form_13f_readme.pdf` loaded 200 through `_fetch_url_bounded` and was decoded as
a string -- PDF bytes that `_non_html_text` refused (`%PDF`) and trafilatura could not read. The fetch now hands a PDF
(by its magic bytes or by `application/pdf`) to the existing extractors, and the reader keeps that text whole.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules.research_agent import _extract_text_and_date
from app.modules.research_extractors import _fetch_url_bounded, is_pdf_text

pytestmark = pytest.mark.smoke

_URL = "http://1.1.1.1/files/form_13f_readme.pdf"


def _pdf(pages: list[str]) -> bytes:
    """A real multi-page PDF in raw PDF syntax (one Helvetica text line per page)."""
    n = len(pages)
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [" + b" ".join(f"{3 + 2 * i} 0 R".encode() for i in range(n))
            + b"] /Count " + str(n).encode() + b" >>"]
    font = 3 + 2 * n
    for i, text in enumerate(pages):
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode("latin-1")
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 {font} 0 R >> >>"
                    f" /Contents {4 + 2 * i} 0 R >>".encode())
        objs.append(b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream")
    objs.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for k, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{k} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


_PAGES = [
    "The information table lists each holding: nameOfIssuer titleOfClass cusip value " * 2,
    "shrsOrPrnAmt sshPrnamt sshPrnamtType investmentDiscretion votingAuthority Sole Shared None " * 2,
]


def _client(body: bytes, content_type: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.headers = {"content-type": content_type}
    resp.url = _URL
    resp.encoding = None

    async def _aiter():
        for i in range(0, len(body), 700):                             # streamed in chunks, like the network
            yield body[i:i + 700]

    resp.aiter_bytes = _aiter
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=False)
    client = MagicMock()
    client.stream = MagicMock(return_value=cm)
    return client


async def _fetch(body: bytes, content_type: str, failure: dict | None = None) -> str | None:
    with patch("app.modules.research_agent.get_generic_http_client", return_value=_client(body, content_type)):
        return await _fetch_url_bounded(_URL, failure=failure)


@pytest.mark.parametrize("content_type", ["application/pdf", "application/octet-stream", "text/plain; charset=utf-8"])
async def test_a_fetched_pdf_is_its_text_every_page(content_type):
    """By the declared type or by the `%PDF-` magic alone (servers mislabel PDFs)."""
    out = await _fetch(_pdf(_PAGES), content_type)
    assert out is not None and is_pdf_text(out)
    assert out.startswith("PDF document, 2 pages, read with ")
    for field in ("nameOfIssuer", "cusip", "sshPrnamtType", "votingAuthority"):   # both pages read
        assert field in out
    assert "endobj" not in out and "%PDF" not in out                  # no raw PDF syntax


async def test_the_reader_keeps_a_pdfs_text_whole():
    """`_extract_text_and_date` reads a PDF's text as it is -- not as a plain-text file cut to its first 40 lines,
    not through trafilatura."""
    pages = [f"Section {i} states field{i} and its meaning in full." for i in range(60)]
    out = await _fetch(_pdf(pages), "application/pdf")
    assert out is not None
    text_out, date = _extract_text_and_date(out)
    assert text_out == out and date == ""
    assert "field0 " in text_out and "field59 " in text_out           # first and last page


async def test_an_unreadable_pdf_is_a_named_failure():
    """A body that claims to be a PDF but has no readable text returns None with its reason (not mojibake)."""
    fail: dict = {}
    assert await _fetch(b"%PDF-1.4\n" + bytes(range(256)) * 8, "application/pdf", failure=fail) is None
    assert fail["reason"] == "pdf_unreadable"


@pytest.mark.parametrize("body,content_type", [
    (b"<!doctype html><html><body><p>PDF document, 3 pages</p></body></html>", "text/html"),
    (b'{"PDF document": 2}', "application/json"),
    (b"Date|Symbol|ShortVolume\n20261120|SPY|1000\n", "text/plain"),
])
async def test_what_is_not_a_pdf_is_fetched_as_before(body, content_type):
    out = await _fetch(body, content_type)
    assert out == body.decode() and not is_pdf_text(out)


def test_a_page_that_only_mentions_a_pdf_is_not_pdf_text():
    assert not is_pdf_text("PDF document, see the attached file.\nMore text")
    assert not is_pdf_text("Intro\nPDF document, 2 pages, read with pypdf; its text:\nx")
