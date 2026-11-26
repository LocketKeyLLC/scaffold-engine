"""§17.2043 — FINRA's daily short-volume file is read in the shape FINRA serves it.

Live (2026-11-26, cdn.finra.org/equity/regsho/daily/CNMSshvol20261125.txt): 6 pipe-separated columns
(`Date|Symbol|ShortVolume|ShortExemptVolume|TotalVolume|Market`), a header row, decimal volumes, and a one-field
record-count trailer. The parser wanted 5 columns, so every real row was dropped and the scheduled run stored 0.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules.paper_finra_data_ingestion import FinraRecord, _direction_for, ingest_finra_file

SAMPLE = Path(__file__).resolve().parents[2] / "fixtures" / "paper_lab" / "finra_CNMSshvol20261125_sample.txt"


class _Response:
    def __init__(self, body: str) -> None:
        self.text = body

    def raise_for_status(self) -> None:
        return None


async def _get(url: str) -> _Response:
    assert url.endswith("/CNMSshvol20261125.txt"), url
    return _Response(SAMPLE.read_text())


@pytest.mark.asyncio
async def test_the_real_daily_file_stores_every_symbol_row() -> None:
    counts = await ingest_finra_file(source="short_interest", release_date=date(2026, 11, 25), version=1,
                                     http_get=_get)
    assert counts == {"ingested_items": 5, "signals": 5}   # 5 rows; the citation, header and trailer are not rows
    async with async_session() as db:
        rows = {r[0]: r[1] for r in (await db.execute(text(
            "SELECT payload->>'symbol', payload FROM ingested_item WHERE source = 'finra'"))).all()}
    assert sorted(rows) == ["AAAB", "AAPL", "MSFT", "NVDA", "SPY"]
    nvda = rows["NVDA"]
    assert nvda["trade_date"] == "20261125"
    assert nvda["short_volume"] == "62739216.368559"
    assert nvda["short_exempt_volume"] == "432715"
    assert nvda["total_volume"] == "164628520.890395"
    assert nvda["market"] == "B,Q,N"


def test_a_row_with_volume_is_long_and_one_with_none_is_flat() -> None:
    traded = FinraRecord("short_interest", date(2026, 11, 25), 1, "AAAB",
                         short_volume="2", short_exempt_volume="0", total_volume="6")
    idle = FinraRecord("short_interest", date(2026, 11, 25), 1, "ZZZZ",
                       short_volume="0", short_exempt_volume="0", total_volume="0")
    assert _direction_for(traded) == "long" and _direction_for(idle) == "flat"
