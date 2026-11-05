# tests/test_paper_congressional_trade_ingestion.py
"""Tests for paper_congressional_trade_ingestion.

All HTTP traffic is mocked via a fake ``httpx.AsyncClient``.  Tests run
against the scratch async SQLAlchemy database and verify the rows that
the module inserts into the append-only ``ingested_item`` table.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import httpx
import pytest

from app.database import async_session
from app.modules.paper_congressional_trade_ingestion import (
    CONGRESS_SOURCE,
    HOUSE_BASE_URL,
    SENATE_BASE_URL,
    DisclosureRow,
    FetchResult,
    fetch_disclosures,
)
from sqlalchemy import text


class FakeResponse:
    def __init__(self, text: str, status_code: int = 200, headers: dict | None = None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPError(f"HTTP {self.status_code}")


class FakeClient:
    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[str] = []

    async def get(self, url: str, *, timeout: float = 30.0) -> FakeResponse:
        self.calls.append(url)
        if not self.responses:
            raise httpx.HTTPError("no more responses")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


async def _count_congress_items() -> int:
    async with async_session() as db:
        result = await db.execute(
            text("SELECT COUNT(*) FROM ingested_item WHERE source = :source"),
            {"source": CONGRESS_SOURCE},
        )
        return result.scalar()


async def _get_congress_items() -> list:
    async with async_session() as db:
        result = await db.execute(
            text("""
                SELECT external_id, payload, published_ts
                FROM ingested_item
                WHERE source = :source
                ORDER BY id
            """),
            {"source": CONGRESS_SOURCE},
        )
        return result.all()


def _payload_to_dict(payload):
    if isinstance(payload, dict):
        return payload
    return json.loads(payload)


@pytest.mark.asyncio
async def test_fetch_disclosures_stores_raw_house_and_senate_responses():
    client = FakeClient([
        FakeResponse(
            "<html>House disclosure landing page</html>",
            headers={"date": "Mon, 04 Nov 2026 23:32:00 GMT"},
        ),
        FakeResponse(
            "<html>Senate eFD search landing page</html>",
            headers={"date": "Tue, 05 Nov 2026 10:00:00 GMT"},
        ),
    ])

    result = await fetch_disclosures(client)

    assert isinstance(result, FetchResult)
    assert len(result.rows) == 2
    assert result.errors == []
    assert HOUSE_BASE_URL in client.calls[0]
    assert SENATE_BASE_URL in client.calls[1]

    assert await _count_congress_items() == 2

    rows = await _get_congress_items()
    chambers = []
    for external_id, payload, published_ts in rows:
        data = _payload_to_dict(payload)
        chambers.append(data["chamber"])
        assert data["chamber"] in ("house", "senate")
        assert data["url"].startswith(
            HOUSE_BASE_URL if data["chamber"] == "house" else SENATE_BASE_URL
        )
        assert "raw_response" in data
        assert data["raw_response"].startswith("<html>")
        if data["chamber"] == "senate":
            assert "5 U.S.C. app. § 105(c)" in data["legal_notice"]
        else:
            assert data["legal_notice"] is None
        assert external_id.startswith(f"{data['chamber']}:")
        assert published_ts is not None

    assert sorted(chambers) == ["house", "senate"]


@pytest.mark.asyncio
async def test_fetch_disclosures_respects_max_requests():
    client = FakeClient([
        FakeResponse("<html>house</html>"),
        FakeResponse("<html>senate</html>"),
    ])

    result = await fetch_disclosures(client, max_requests=1)

    assert len(result.rows) == 1
    assert len(result.errors) == 1
    assert "max_requests" in result.errors[0]
    assert await _count_congress_items() == 1


@pytest.mark.asyncio
async def test_fetch_disclosures_records_http_errors_without_crashing():
    client = FakeClient([
        FakeResponse("<html>house</html>"),
        httpx.HTTPError("senate timeout"),
    ])

    result = await fetch_disclosures(client)

    assert len(result.rows) == 1
    assert len(result.errors) == 1
    assert "senate" in result.errors[0]
    assert await _count_congress_items() == 1


@pytest.mark.asyncio
async def test_fetch_disclosures_is_idempotent_for_same_url():
    house_response = FakeResponse("<html>house</html>")
    senate_response = FakeResponse("<html>senate</html>")

    await fetch_disclosures(FakeClient([house_response, senate_response]))
    await fetch_disclosures(FakeClient([house_response, senate_response]))

    assert await _count_congress_items() == 2


@pytest.mark.asyncio
async def test_fetch_disclosures_uses_last_modified_header_for_published_ts():
    last_modified = "Wed, 06 Nov 2026 08:15:30 GMT"
    client = FakeClient([
        FakeResponse(
            "<html>house</html>",
            headers={"last-modified": last_modified, "date": "Wed, 06 Nov 2026 09:00:00 GMT"},
        ),
        FakeResponse("<html>senate</html>"),
    ])

    await fetch_disclosures(client)

    rows = await _get_congress_items()
    house_published = rows[0][2]
    expected = datetime(2026, 11, 6, 8, 15, 30, tzinfo=timezone.utc)
    assert house_published == expected


@pytest.mark.asyncio
async def test_fetch_disclosures_uses_date_header_when_last_modified_missing():
    date_header = "Thu, 07 Nov 2026 12:00:00 GMT"
    client = FakeClient([
        FakeResponse("<html>house</html>", headers={"date": date_header}),
        FakeResponse("<html>senate</html>"),
    ])

    await fetch_disclosures(client)

    rows = await _get_congress_items()
    house_published = rows[0][2]
    expected = datetime(2026, 11, 7, 12, 0, 0, tzinfo=timezone.utc)
    assert house_published == expected
