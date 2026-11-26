"""§17.2044 — the weekly ATS volume comes from where FINRA serves it, on the lab's weekly run.

Live (2026-11-26): the URL the lab built, cdn.finra.org/ats/weekly/ATSvolume<date>.txt, answers 403 -- FINRA serves no
such file. Per-symbol weekly ATS totals are FINRA's public Query API, `otcMarket/weeklySummary` (no key), summary type
`ATS_W_SMBL`, published about three weeks after the week (the week of 2026-11-02 appeared 2026-11-23).
"""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules import paper_finra_data_ingestion as finra
from app.modules import paper_lab_schedule as sched

SAMPLE = json.loads((Path(__file__).resolve().parents[2] / "fixtures" / "paper_lab"
                     / "finra_ats_weekly_summary_2026-11-26.json").read_text())


class _Response:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def raise_for_status(self) -> None:
        return None

    def json(self) -> list:
        return self._rows


class _Post:
    """FINRA's API as it answered: the recorded rows on the first page, nothing after."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    async def __call__(self, url: str, *, json: dict, headers: dict) -> _Response:
        self.calls.append((url, json))
        return _Response(SAMPLE["response"] if json.get("offset", 0) == 0 else [])


@pytest.mark.asyncio
async def test_the_weekly_ats_rows_are_stored_per_symbol_and_week() -> None:
    post = _Post()
    counts = await finra.ingest_finra_ats_weekly(symbols=["SPY", "AAPL"], until=date(2026, 11, 26), http_post=post)
    assert counts == {"ingested_items": 4, "signals": 4}
    url, body = post.calls[0]
    assert url == "https://api.finra.org/data/group/otcMarket/name/weeklySummary"
    assert {"compareType": "EQUAL", "fieldName": "summaryTypeCode", "fieldValue": "ATS_W_SMBL"} in body["compareFilters"]
    assert body["domainFilters"] == [{"fieldName": "issueSymbolIdentifier", "values": ["AAPL", "SPY"]}]
    window = body["dateRangeFilters"][0]
    assert window["fieldName"] == "weekStartDate" and window["endDate"] == "2026-11-26"
    assert window["startDate"] <= "2026-10-26"            # covers the ~3-week publication delay
    async with async_session() as db:
        rows = (await db.execute(text(
            "SELECT external_id, payload FROM ingested_item WHERE source = 'finra' ORDER BY external_id"))).all()
    ids = [r[0] for r in rows]
    assert ids == ["2026-10-26:AAPL:ats_volume:1", "2026-10-26:SPY:ats_volume:1",
                   "2026-11-02:AAPL:ats_volume:1", "2026-11-02:SPY:ats_volume:1"]
    aapl = dict(rows)["2026-11-02:AAPL:ats_volume:1"]
    assert aapl["ats_volume"] == "62664057" and aapl["trade_count"] == "945431"
    assert aapl["week_start_date"] == "2026-11-02" and aapl["tier"] == "T1"
    again = await finra.ingest_finra_ats_weekly(symbols=["AAPL", "SPY"], until=date(2026, 11, 26), http_post=_Post())
    assert again == {"ingested_items": 0, "signals": 0}   # a week already stored is not stored twice


def test_no_url_is_built_for_a_file_finra_does_not_serve() -> None:
    with pytest.raises(ValueError, match="weekly ATS"):
        finra._build_url("ats_volume", date(2026, 11, 20), 1)


@pytest.mark.asyncio
async def test_the_weekly_run_ingests_the_ats_weeks() -> None:
    got = AsyncMock(return_value={"ingested_items": 4, "signals": 4})
    with patch.object(sched, "_sec_contact_email", AsyncMock(return_value=None)), \
            patch.object(sched, "ingest_finra_ats_weekly", got), \
            patch.object(sched, "re_evaluate_strategy", AsyncMock(return_value={"decision": "keep"})), \
            patch.object(sched, "settle_strategy", AsyncMock(return_value={"asked": None})):
        report = await sched.run_weekly(now=datetime(2026, 11, 28, 12, 0, tzinfo=timezone.utc))
    assert report["finra_ats"]["status"] == "ok"
    kwargs = got.await_args.kwargs
    assert kwargs["until"] == date(2026, 11, 28) and set(kwargs["symbols"]) == set(sched.ALLOW_LIST)
