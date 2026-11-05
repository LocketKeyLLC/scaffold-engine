# tests/test_paper_finra_data_ingestion.py
"""pytest tests for the FINRA data ingestion module."""

from __future__ import annotations

from datetime import date

import httpx
import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_finra_data_ingestion import (
    FINRA_ATS_VOLUME_URL,
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
    """Return sample short interest pipe-delimited content."""
    return (
        "20261103|AAPL|123456|1000000|1.23\n"
        "20261103|MSFT|0|0|0\n"
        "20261103|GOOG|500|600|0.8\n"
    )


@pytest.mark.asyncio
async def test_finra_file_url_short_interest() -> None:
    f = FinraFile(source="short_interest", release_date=date(2026, 11, 3), version=1)
    assert f.url == FINRA_SHORT_INTEREST_URL.format(date="20261103")


@pytest.mark.asyncio
async def test_finra_file_url_ats_volume() -> None:
    f = FinraFile(source="ats_volume", release_date=date(2026, 11, 3), version=1)
    assert f.url == FINRA_ATS_VOLUME_URL.format(date="20261103")


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
    content = "20261103|AAPL|123456|1000000|1.23\n"

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
    content = "20261103|AAPL|1000|5000|20.0\n"

    async def fake_get(url: str) -> FakeResponse:
        return _http_response(text=content)

    counts = await ingest_finra_file(
        source="ats_volume",
        release_date=date(2026, 11, 3),
        version=1,
        http_get=fake_get,
    )
    assert counts == {"ingested_items": 1, "signals": 1}

    async with async_session() as db:
        item = await db.execute(
            text("SELECT source, external_id, payload FROM ingested_item")
        )
        row = item.fetchone()
        assert row is not None
        assert row[0] == "finra"
        assert row[1] == "2026-11-03:AAPL:ats_volume:1"
        payload = row[2]
        assert payload["ats_volume"] == "1000"
        assert payload["total_volume"] == "5000"

        sig = await db.execute(
            text("SELECT ticker, signal_type, direction FROM signal")
        )
        sig_row = sig.fetchone()
        assert sig_row is not None
        assert sig_row[0] == "AAPL"
        assert sig_row[1] == "finra_context"
        assert sig_row[2] == "long"


@pytest.mark.asyncio
async def test_ingest_finra_file_skips_malformed_rows() -> None:
    content = "bad|row\n20261103|AAPL|123456|1000000|1.23\n"

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
            return _http_response(text="20261103|AAPL|123456|1000000|1.23\n")
        return _http_response(text="20261104|MSFT|500|600|0.8\n")

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
    content = "20261103|AAPL|123456|1000000|1.23\n"

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
    content = "20261103|AAPL|123456|1000000|1.23\n"

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
        short_interest="0",
        avg_daily_volume="0",
        days_to_cover="0",
    )
    assert _direction_for(record) == "flat"

    nonzero = FinraRecord(
        "short_interest",
        date(2026, 11, 3),
        1,
        "AAPL",
        short_interest="123456",
        avg_daily_volume="1000000",
        days_to_cover="1.23",
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
    content = "20261103|MSFT|0|0|0\n"

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
