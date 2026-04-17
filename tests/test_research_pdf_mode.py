"""Behavioral tests for PDF-mode research (/research/pdf endpoint + helpers)."""
import io
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import research_agent as ra


# ---------------------------------------------------------------------------
# _extract_threshold
# ---------------------------------------------------------------------------

class TestExtractThreshold:
    def test_floor_is_200(self):
        assert ra._extract_threshold(0) == 200
        assert ra._extract_threshold(1) == 200
        assert ra._extract_threshold(3) == 200  # 3*50=150 < 200

    def test_scales_with_pages(self):
        assert ra._extract_threshold(10) == 500
        assert ra._extract_threshold(50) == 2500
        assert ra._extract_threshold(100) == 5000


# ---------------------------------------------------------------------------
# _extract_pypdf / _extract_pdfplumber — generate minimal PDFs inline
# ---------------------------------------------------------------------------

# Minimal valid 1-page PDF with literal text. Built once and cached to avoid
# a reportlab test dep. Adjustable via the `text` arg for tests that need
# specific content.
def _make_test_pdf(text: str = "Hello Scaffold Engine test document content.") -> bytes:
    """Build a tiny 1-page PDF using raw PDF syntax. No external deps."""
    # Escape PDF string literal: \ -> \\, ( -> \(, ) -> \)
    esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    content = f"BT /F1 12 Tf 72 720 Td ({esc}) Tj ET"
    stream = content.encode("latin-1", errors="replace")

    objs = []
    objs.append(b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n")
    objs.append(b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n")
    objs.append(
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>\nendobj\n"
    )
    objs.append(
        b"4 0 obj\n<< /Length " + str(len(stream)).encode() + b" >>\nstream\n"
        + stream + b"\nendstream\nendobj\n"
    )
    objs.append(b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n")

    out = b"%PDF-1.4\n"
    offsets = [0]
    for o in objs:
        offsets.append(len(out))
        out += o
    xref_pos = len(out)
    out += b"xref\n0 6\n0000000000 65535 f \n"
    for off in offsets[1:]:
        out += f"{off:010d} 00000 n \n".encode()
    out += b"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n"
    out += str(xref_pos).encode() + b"\n%%EOF"
    return out


class TestExtractPypdf:
    def test_extracts_text(self):
        pdf = _make_test_pdf("Milvus is a vector database.")
        text, pages = ra._extract_pypdf(pdf)
        assert pages == 1
        assert "Milvus" in text
        assert "vector database" in text

    def test_returns_page_count(self):
        pdf = _make_test_pdf("single page")
        _, pages = ra._extract_pypdf(pdf)
        assert pages == 1


class TestExtractPdfplumber:
    def test_extracts_text(self):
        pdf = _make_test_pdf("pdfplumber extraction works.")
        text, pages = ra._extract_pdfplumber(pdf)
        assert pages == 1
        assert "pdfplumber" in text or "extraction" in text


# ---------------------------------------------------------------------------
# _extract_pdf_text — cascade logic
# ---------------------------------------------------------------------------

class TestExtractPdfTextCascade:
    @pytest.mark.asyncio
    async def test_pypdf_succeeds_no_fallback(self):
        pdf = _make_test_pdf("Enough text here to pass the threshold easily. " * 20)
        text, pages, used = await ra._extract_pdf_text(pdf, extractor="auto")
        assert used == "pypdf"
        assert len(text) >= 200

    @pytest.mark.asyncio
    async def test_auto_falls_back_to_plumber(self):
        """pypdf returns too little → auto uses plumber."""
        pdf = _make_test_pdf("good content here for plumber to extract. " * 20)
        fake_pypdf_short = ("tiny", 1)
        fake_plumber_long = ("enough text to pass threshold " * 20, 1)
        with patch.object(ra, "_extract_pypdf", return_value=fake_pypdf_short), \
             patch.object(ra, "_extract_pdfplumber", return_value=fake_plumber_long):
            text, pages, used = await ra._extract_pdf_text(pdf, extractor="auto")
            assert used == "plumber"
            assert len(text) >= 200

    @pytest.mark.asyncio
    async def test_force_pypdf_no_fallback(self):
        """extractor=pypdf forces pypdf even when output is short."""
        with patch.object(ra, "_extract_pypdf", return_value=("tiny", 1)), \
             patch.object(ra, "_extract_pdfplumber") as plumber_mock:
            text, pages, used = await ra._extract_pdf_text(b"fake", extractor="pypdf")
            assert used == "pypdf"
            plumber_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_force_plumber_skips_pypdf(self):
        with patch.object(ra, "_extract_pypdf") as pypdf_mock, \
             patch.object(ra, "_extract_pdfplumber", return_value=("long " * 50, 1)):
            text, pages, used = await ra._extract_pdf_text(b"fake", extractor="plumber")
            assert used == "plumber"
            pypdf_mock.assert_not_called()

    @pytest.mark.asyncio
    async def test_both_fail_raises_scanned_error(self):
        with patch.object(ra, "_extract_pypdf", return_value=("", 5)), \
             patch.object(ra, "_extract_pdfplumber", return_value=("", 5)):
            with pytest.raises(RuntimeError) as exc:
                await ra._extract_pdf_text(b"fake", extractor="auto")
            assert "scanned" in str(exc.value).lower() or "unreadable" in str(exc.value).lower()

    @pytest.mark.asyncio
    async def test_invalid_extractor_defaults_to_auto(self):
        """Invalid extractor name falls back to auto behavior."""
        pdf = _make_test_pdf("content " * 50)
        text, pages, used = await ra._extract_pdf_text(pdf, extractor="garbage")
        assert used in ("pypdf", "plumber")


# ---------------------------------------------------------------------------
# run_research_pdf — E2E happy path
# ---------------------------------------------------------------------------

def _parse_sse(raw_events: list[str]) -> list[tuple[str, dict]]:
    out = []
    for blob in raw_events:
        etype = None
        data = ""
        for line in blob.splitlines():
            if line.startswith("event:"):
                etype = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if etype:
            try:
                out.append((etype, json.loads(data) if data else {}))
            except json.JSONDecodeError:
                out.append((etype, {}))
    return out


class TestRunResearchPdf:
    @pytest.mark.asyncio
    async def test_happy_path_emits_complete(self):
        pdf_bytes = _make_test_pdf("Milvus vector DB content. " * 30)
        fake_llm = MagicMock(
            success=True,
            text='[{"title":"Milvus","content":"Milvus is a vector DB.","tags":"","source":"pdf://x.pdf","source_type":"tech_docs"}]',
            error=None,
        )

        with patch.object(ra, "_guard_concurrent", AsyncMock(return_value=None)), \
             patch.object(ra, "_create_session", AsyncMock(return_value=100)), \
             patch.object(ra.model_router, "generate", AsyncMock(return_value=fake_llm)), \
             patch.object(ra, "ingest_entries", AsyncMock(return_value={"new": 1, "versioned": 0, "rejected": 0, "skipped_hash": 0})), \
             patch.object(ra, "_generate_summary", AsyncMock(return_value="summary")), \
             patch.object(ra, "_update_session_iteration", AsyncMock()), \
             patch.object(ra, "_finalize_session", AsyncMock()):

            events = []
            async for blob in ra.run_research_pdf(pdf_bytes, filename="test.pdf"):
                events.append(blob)

            parsed = _parse_sse(events)
            etypes = [e for e, _ in parsed]

            assert "research_started" in etypes
            assert "research_complete" in etypes
            started = dict(parsed)["research_started"]
            assert started["mode"] == "direct_pdf"
            assert started["bytes"] == len(pdf_bytes)
            complete = dict(parsed)["research_complete"]
            assert complete["depth"] == "direct_pdf"
            assert complete["iterations"] == 1
            assert complete["extractor_used"] == "pypdf"
            assert complete["page_count"] == 1

    @pytest.mark.asyncio
    async def test_oversize_pdf_rejected(self):
        big_pdf = b"X" * (21 * 1024 * 1024)  # 21 MB
        with patch.object(ra, "_guard_concurrent", AsyncMock(return_value=None)), \
             patch.object(ra, "_create_session", AsyncMock(return_value=101)), \
             patch.object(ra, "_finalize_session", AsyncMock()):

            events = []
            async for blob in ra.run_research_pdf(big_pdf, filename="big.pdf"):
                events.append(blob)

            parsed = _parse_sse(events)
            etypes = [e for e, _ in parsed]
            assert "error" in etypes
            assert "research_complete" not in etypes
            err = dict(parsed)["error"]
            assert "20" in err["message"] or "MB" in err["message"] or "cap" in err["message"].lower()

    @pytest.mark.asyncio
    async def test_scanned_pdf_emits_error(self):
        pdf_bytes = _make_test_pdf("x " * 5)
        with patch.object(ra, "_guard_concurrent", AsyncMock(return_value=None)), \
             patch.object(ra, "_create_session", AsyncMock(return_value=102)), \
             patch.object(ra, "_extract_pdf_text", AsyncMock(side_effect=RuntimeError("PDF appears to be scanned"))), \
             patch.object(ra, "_finalize_session", AsyncMock()):

            events = []
            async for blob in ra.run_research_pdf(pdf_bytes, filename="scanned.pdf"):
                events.append(blob)

            parsed = _parse_sse(events)
            etypes = [e for e, _ in parsed]
            assert "error" in etypes
            err = dict(parsed)["error"]
            assert "scanned" in err["message"].lower()

    @pytest.mark.asyncio
    async def test_concurrent_research_blocked(self):
        existing = {"id": "xxx", "topic": "other research"}
        with patch.object(ra, "_guard_concurrent", AsyncMock(return_value=existing)):
            events = []
            async for blob in ra.run_research_pdf(b"fake", filename="blocked.pdf"):
                events.append(blob)

            parsed = _parse_sse(events)
            etypes = [e for e, _ in parsed]
            assert "error" in etypes
            err = dict(parsed)["error"]
            assert err.get("http_status") == 409

    @pytest.mark.asyncio
    async def test_extractor_param_propagates(self):
        """Passing extractor='plumber' reaches _extract_pdf_text."""
        pdf_bytes = _make_test_pdf("content " * 50)
        extract_mock = AsyncMock(return_value=("extracted text " * 40, 1, "plumber"))
        fake_llm = MagicMock(success=True, text='[]', error=None)

        with patch.object(ra, "_guard_concurrent", AsyncMock(return_value=None)), \
             patch.object(ra, "_create_session", AsyncMock(return_value=103)), \
             patch.object(ra, "_extract_pdf_text", extract_mock), \
             patch.object(ra.model_router, "generate", AsyncMock(return_value=fake_llm)), \
             patch.object(ra, "ingest_entries", AsyncMock(return_value={"new": 0, "versioned": 0, "rejected": 0, "skipped_hash": 0})), \
             patch.object(ra, "_generate_summary", AsyncMock(return_value="s")), \
             patch.object(ra, "_update_session_iteration", AsyncMock()), \
             patch.object(ra, "_finalize_session", AsyncMock()):

            async for _ in ra.run_research_pdf(pdf_bytes, filename="x.pdf", extractor="plumber"):
                pass

            # _extract_pdf_text called with extractor="plumber"
            assert extract_mock.call_args.kwargs.get("extractor") == "plumber"

    @pytest.mark.asyncio
    async def test_domain_override(self):
        pdf_bytes = _make_test_pdf("content " * 50)
        create_session_mock = AsyncMock(return_value=104)
        ingest_mock = AsyncMock(return_value={"new": 1, "versioned": 0, "rejected": 0, "skipped_hash": 0})
        fake_llm = MagicMock(success=True, text='[{"title":"T","content":"C","tags":"","source":"pdf://x.pdf","source_type":"tech_docs"}]', error=None)

        with patch.object(ra, "_guard_concurrent", AsyncMock(return_value=None)), \
             patch.object(ra, "_create_session", create_session_mock), \
             patch.object(ra.model_router, "generate", AsyncMock(return_value=fake_llm)), \
             patch.object(ra, "ingest_entries", ingest_mock), \
             patch.object(ra, "_generate_summary", AsyncMock(return_value="s")), \
             patch.object(ra, "_update_session_iteration", AsyncMock()), \
             patch.object(ra, "_finalize_session", AsyncMock()):

            async for _ in ra.run_research_pdf(pdf_bytes, filename="x.pdf", domain="spec"):
                pass

            # session created with spec domain
            args = create_session_mock.call_args.args
            assert "spec" in args

            # ingest called with spec domain
            ingest_kwargs = ingest_mock.call_args.kwargs
            assert ingest_kwargs.get("domain") == "spec"
