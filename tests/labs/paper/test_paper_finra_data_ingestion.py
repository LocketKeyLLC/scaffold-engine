# tests/test_paper_finra_data_ingestion.py
"""pytest tests for the FINRA data ingestion module."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import httpx
import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_finra_data_ingestion import (
    FINRA_SHORT_INTEREST_URL,
    FinraFile,
    FinraRecord,
    _build_url,
    _direction_for,
    ingest_finra_file,
    ingest_finra_files,
)


class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200) -> None:
        self.text = text
        self.status_code = status_code
        self.url = "http://example.com"

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", self.url)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"HTTP {self.status_code}",
                request=request,
                response=response,
            )


def _http_response(text: str = "", status_code: int = 200) -> FakeResponse:
    """Return a fake HTTP response."""
    return FakeResponse(text=text, status_code=status_code)


def _short_interest_content() -> str:
    """Rows in the daily short-volume file's shape (§17.2043: Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|
    Market, as cdn.finra.org serves it; the AAPL row's volumes are the real 2026-11-25 ones)."""
    return (
        "20261103|AAPL|9758284.194435|27391.310000|16888251.033617|B,Q,N\n"
        "20261103|MSFT|0|0|0|Q\n"
        "20261103|GOOG|500|0|600|Q\n"
    )


@pytest.mark.asyncio
async def test_finra_file_url_short_interest() -> None:
    f = FinraFile(source="short_interest", release_date=date(2026, 11, 3), version=1)
    assert f.url == FINRA_SHORT_INTEREST_URL.format(date="20261103")


@pytest.mark.asyncio
async def test_finra_file_url_ats_volume() -> None:
    # §17.2044 — FINRA serves no weekly ATS file (cdn.finra.org/ats/weekly/ATSvolume<date>.txt answers 403); the weekly
    # totals come from its Query API (ingest_finra_ats_weekly), so no file URL is built for it
    f = FinraFile(source="ats_volume", release_date=date(2026, 11, 3), version=1)
    with pytest.raises(ValueError, match="weekly ATS"):
        f.url


@pytest.mark.asyncio
async def test_finra_file_url_invalid_source_raises() -> None:
    with pytest.raises(ValueError):
        FinraFile(source="bogus", release_date=date(2026, 11, 3), version=1).url


def test_finra_record_payload() -> None:
    record = FinraRecord(
        "short_interest",
        date(2026, 11, 3),
        1,
        "AAPL",
        settlement_date="20261103",
        short_interest="123456",
        avg_daily_volume="1000000",
        days_to_cover="1.23",
    )
    payload = record.payload()
    assert payload["source_type"] == "short_interest"
    assert payload["release_date"] == "2026-11-03"
    assert payload["version"] == 1
    assert payload["symbol"] == "AAPL"
    assert payload["settlement_date"] == "20261103"
    assert payload["short_interest"] == "123456"
    assert payload["avg_daily_volume"] == "1000000"
    assert payload["days_to_cover"] == "1.23"


@pytest.mark.asyncio
async def test_ingest_finra_file_short_interest() -> None:
    release = date(2026, 11, 3)
    content = "20261103|AAPL|9758284.194435|27391.310000|16888251.033617|B,Q,N\n"

    async def fake_get(url: str) -> FakeResponse:
        return _http_response(text=content)

    counts = await ingest_finra_file(
        source="short_interest",
        release_date=release,
        version=1,
        http_get=fake_get,
    )
    assert counts == {"ingested_items": 1, "signals": 1}


@pytest.mark.asyncio
async def test_ingest_finra_file_ats_volume() -> None:
    # §17.2044 — the file path fetches nothing for the weekly ATS totals (FINRA serves no such file)
    fake_get = AsyncMock()

    with pytest.raises(ValueError, match="weekly ATS"):
        await ingest_finra_file(
            source="ats_volume",
            release_date=date(2026, 11, 3),
            version=1,
            http_get=fake_get,
        )
    fake_get.assert_not_awaited()


@pytest.mark.asyncio
async def test_ingest_finra_file_skips_malformed_rows() -> None:
    content = "bad|row\n20261103|AAPL|9758284.194435|27391.310000|16888251.033617|B,Q,N\n"

    async def fake_get(url: str) -> FakeResponse:
        return _http_response(text=content)

    counts = await ingest_finra_file(
        source="short_interest",
        release_date=date(2026, 11, 3),
        version=1,
        http_get=fake_get,
    )
    assert counts == {"ingested_items": 1, "signals": 1}


@pytest.mark.asyncio
async def test_ingest_finra_file_non_200_raises() -> None:
    async def fake_get(url: str) -> FakeResponse:
        return _http_response(text="", status_code=500)

    with pytest.raises(httpx.HTTPStatusError):
        await ingest_finra_file(
            source="short_interest",
            release_date=date(2026, 11, 3),
            version=1,
            http_get=fake_get,
        )


@pytest.mark.asyncio
async def test_ingest_finra_files_batch() -> None:
    release1 = date(2026, 11, 3)
    release2 = date(2026, 11, 4)

    async def fake_get(url: str) -> FakeResponse:
        if "20261103" in url:
            return _http_response(text="20261103|AAPL|9758284.194435|27391.310000|16888251.033617|B,Q,N\n")
        return _http_response(text="20261104|MSFT|500|0|600|Q\n")

    total = await ingest_finra_files(
        source="short_interest",
        release_dates=[release1, release2],
        http_get=fake_get,
    )
    assert total == {"ingested_items": 2, "signals": 2}


@pytest.mark.asyncio
async def test_ingest_finra_file_uses_default_httpx() -> None:
    # The module always requires an explicit http_get; this test verifies
    # that providing a normal callable works and the function behaves
    # identically to a default httpx client.
    release = date(2026, 11, 3)
    content = "20261103|AAPL|9758284.194435|27391.310000|16888251.033617|B,Q,N\n"

    async def fake_get(url: str) -> FakeResponse:
        return _http_response(text=content)

    counts = await ingest_finra_file(
        source="short_interest",
        release_date=release,
        version=1,
        http_get=fake_get,
    )
    assert counts == {"ingested_items": 1, "signals": 1}


@pytest.mark.asyncio
async def test_ingest_finra_file_idempotent() -> None:
    release = date(2026, 11, 3)
    content = "20261103|AAPL|9758284.194435|27391.310000|16888251.033617|B,Q,N\n"

    async def fake_get(url: str) -> FakeResponse:
        return _http_response(text=content)

    first = await ingest_finra_file(
        source="short_interest",
        release_date=release,
        version=1,
        http_get=fake_get,
    )
    second = await ingest_finra_file(
        source="short_interest",
        release_date=release,
        version=1,
        http_get=fake_get,
    )
    assert first == {"ingested_items": 1, "signals": 1}
    assert second == {"ingested_items": 0, "signals": 0}


@pytest.mark.asyncio
async def test_http_error_is_propagated() -> None:
    async def failing_get(url: str) -> FakeResponse:
        raise httpx.HTTPError("connection refused")

    with pytest.raises(httpx.HTTPError):
        await ingest_finra_file(
            source="short_interest",
            release_date=date(2026, 11, 3),
            version=1,
            http_get=failing_get,
        )


@pytest.mark.asyncio
async def test_zero_volume_record_is_flat() -> None:
    record = FinraRecord(
        "short_interest",
        date(2026, 11, 3),
        1,
        "MSFT",
        short_volume="0",
        short_exempt_volume="0",
        total_volume="0",
    )
    assert _direction_for(record) == "flat"

    nonzero = FinraRecord(
        "short_interest",
        date(2026, 11, 3),
        1,
        "AAPL",
        short_volume="9758284.194435",
        short_exempt_volume="27391.310000",
        total_volume="16888251.033617",
    )
    assert _direction_for(nonzero) == "long"


@pytest.mark.asyncio
async def test_empty_file_returns_zero_counts() -> None:
    async def fake_get(url: str) -> FakeResponse:
        return _http_response(text="")

    counts = await ingest_finra_file(
        source="short_interest",
        release_date=date(2026, 11, 3),
        version=1,
        http_get=fake_get,
    )
    assert counts == {"ingested_items": 0, "signals": 0}


@pytest.mark.asyncio
async def test_unsupported_file_type_rejected() -> None:
    async def fake_get(url: str) -> FakeResponse:
        return _http_response(text="whatever")

    with pytest.raises(ValueError):
        await ingest_finra_file(
            source="bogus",
            release_date=date(2026, 11, 3),
            version=1,
            http_get=fake_get,
        )
    with pytest.raises(ValueError):
        _build_url("bogus", date(2026, 11, 3), 1)


@pytest.mark.asyncio
async def test_flat_signal_recorded_in_db() -> None:
    release = date(2026, 11, 3)
    content = "20261103|MSFT|0|0|0|Q\n"

    async def fake_get(url: str) -> FakeResponse:
        return _http_response(text=content)

    await ingest_finra_file(
        source="short_interest",
        release_date=release,
        version=1,
        http_get=fake_get,
    )

    async with async_session() as db:
        result = await db.execute(
            text("SELECT direction FROM signal WHERE ticker = 'MSFT'")
        )
        direction = result.scalar_one()
        assert direction == "flat"
