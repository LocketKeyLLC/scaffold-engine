"""§17.2041 — the paper lab's recurring runs (app/modules/paper_lab_schedule.py) and the approved order placed on the
operator's answer. Every network call is a fake; the lab's tables are real (the lab conftest empties them)."""
from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text

from app.database import async_session
from app.modules import operator_ask
from app.modules import paper_13f_holdings_ingestion as thirteen_f
from app.modules import paper_lab_schedule as pls
from app.modules import paper_operator_approval_gate as gate
from app.modules.paper_alpaca_paper_client import AlpacaSecretsMissingError
from app.modules.paper_point_time_store import get_data_as_of
from app.modules.paper_reddit_ingestion import RedditSecretsMissingError

NOW = datetime(2026, 11, 24, 11, 0, tzinfo=timezone.utc)  # a Tuesday


class FakeResponse:
    def __init__(self, *, text_body: str = "", json_body=None, status: int = 200):
        self.text = text_body
        self.content = text_body.encode()
        self._json = json_body
        self.status_code = status
        self.headers = {"last-modified": "Mon, 23 Nov 2026 20:00:00 GMT"}

    def json(self):
        return self._json

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeClient:
    """Answers by URL; records every URL asked for."""

    def __init__(self, routes):
        self.routes = routes
        self.urls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kwargs):
        self.urls.append(url)
        for prefix, response in self.routes.items():
            if url.startswith(prefix):
                return response
        return FakeResponse(status=404)


def _client_factory(client):
    return lambda *a, **kw: client


FINRA_DAY = "https://cdn.finra.org/equity/regsho/daily/CNMSshvol20261123.txt"
ROUTES = {
    FINRA_DAY: FakeResponse(text_body="20261123|AAPL|123456|1000000|1.23\n20261123|MSFT|222|3000|0.5\n"),
    "https://disclosures-clerk.house.gov": FakeResponse(text_body="<html>house</html>"),
    "https://efdsearch.senate.gov": FakeResponse(text_body="<html>senate</html>"),
}


@pytest.fixture
def no_sec_email(monkeypatch):
    monkeypatch.setattr(pls, "_sec_contact_email", AsyncMock(return_value=None))


@pytest.fixture
def quiet_news_and_reddit(monkeypatch):
    monkeypatch.setattr(pls, "ingest_news_batch", AsyncMock(return_value={"AAPL": [{"id": 1}], "MSFT": []}))
    monkeypatch.setattr(pls, "ingest_reddit_posts", AsyncMock(return_value=3))


@pytest.fixture
def strategy_jobs():
    """The strategy questions live in the engine's jobs table, which the lab conftest does not empty."""
    async def _drop():
        import asyncpg

        from app.config import settings
        conn = await asyncpg.connect(settings.database_url.replace("postgresql+asyncpg://", "postgresql://", 1))
        try:
            await conn.execute("DELETE FROM jobs WHERE metadata->>'on_answer' = $1", pls.STRATEGY_HANDLER)
        finally:
            await conn.close()
    import asyncio
    asyncio.run(_drop())
    yield
    asyncio.run(_drop())


@pytest.fixture
def ask_without_running(monkeypatch):
    """open_decision_job as is, without starting the executor on the new job."""
    real = operator_ask.open_decision_job

    async def _open(db, **kw):
        kw["start"] = False
        return await real(db, **kw)
    monkeypatch.setattr(pls, "open_decision_job", _open)


async def _answer(job_id: str, choice: str) -> None:
    async with async_session() as db:
        await db.execute(text(
            "UPDATE dag_nodes SET status = 'done', output_text = :o WHERE job_id = :j AND node_key = :k"),
            {"o": f"Decision (operator): {choice}", "j": job_id, "k": pls.STRATEGY_DECISION_KEY})
        await db.commit()


# ── daily ingestion ────────────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_daily_ingestion_fills_the_point_in_time_store_the_batch_reads(
        monkeypatch, no_sec_email, quiet_news_and_reddit):
    client = FakeClient(ROUTES)
    monkeypatch.setattr(pls.httpx, "AsyncClient", _client_factory(client))

    report = await pls.run_daily_ingestion(now=NOW)

    assert report["finra"]["status"] == "ok"
    assert report["finra"]["detail"] == {"ingested_items": 2, "signals": 2}
    assert report["congress"]["status"] == "ok" and report["congress"]["detail"]["stored"] == 2
    assert FINRA_DAY in client.urls  # the previous weekday's file
    items = await get_data_as_of(datetime(2026, 11, 25, tzinfo=timezone.utc))
    assert {p.get("symbol") for p in items if p.get("symbol")} == {"AAPL", "MSFT"}
    assert sum(1 for p in items if p.get("chamber")) == 2


@pytest.mark.asyncio
async def test_without_keys_each_source_says_what_is_missing_and_the_rest_run(
        monkeypatch, no_sec_email, quiet_news_and_reddit):
    monkeypatch.setattr(pls.httpx, "AsyncClient", _client_factory(FakeClient(ROUTES)))
    monkeypatch.setattr(pls, "ingest_reddit_posts",
                        AsyncMock(side_effect=RedditSecretsMissingError("Missing Reddit secrets: REDDIT_CLIENT_ID")))
    monkeypatch.setattr(pls, "ingest_finra_files", AsyncMock(side_effect=RuntimeError("HTTP 404")))

    report = await pls.run_daily_ingestion(now=NOW)

    assert report["form_4"]["status"] == "unavailable"
    assert "SEC_CONTACT_EMAIL" in report["form_4"]["detail"]
    assert report["reddit"] == {"status": "unavailable", "detail": "Missing Reddit secrets: REDDIT_CLIENT_ID"}
    assert report["finra"]["status"] == "failed"
    assert report["congress"]["status"] == "ok"
    assert report["news"] == {"status": "ok", "detail": {"AAPL": 1, "MSFT": 0}}


@pytest.mark.asyncio
async def test_form_4_reads_each_allow_list_company_by_its_sec_cik(monkeypatch, quiet_news_and_reddit):
    monkeypatch.setattr(pls, "_sec_contact_email", AsyncMock(return_value="ops@lab.invalid"))
    monkeypatch.setattr(pls, "ALLOW_LIST", frozenset({"AAPL", "BRK.B", "SPY", "MSFT"}))
    tickers = {"0": {"cik_str": 320193, "ticker": "AAPL"}, "1": {"cik_str": 1067983, "ticker": "BRK-B"},
               "2": {"cik_str": 789019, "ticker": "MSFT"}}
    client = FakeClient({pls.SEC_COMPANY_TICKERS_URL: FakeResponse(json_body=tickers), **ROUTES})
    monkeypatch.setattr(pls.httpx, "AsyncClient", _client_factory(client))
    seen: list[tuple[int, str, int]] = []

    async def fake_form4(db, cik, *, client, contact_email, max_filings):
        seen.append((cik, contact_email, max_filings))
        if cik == 789019:
            raise RuntimeError("HTTP 503")
        return 2
    monkeypatch.setattr(pls, "ingest_form_4_filings", fake_form4)

    report = await pls.run_daily_ingestion(now=NOW)

    assert sorted(c for c, _, _ in seen) == [320193, 789019, 1067983]  # BRK.B as SEC's BRK-B; SPY not listed
    assert {e for _, e, _ in seen} == {"ops@lab.invalid"}
    assert {m for _, _, m in seen} == {pls.FORM4_FILINGS_PER_COMPANY}
    detail = report["form_4"]["detail"]
    assert report["form_4"]["status"] == "ok"
    assert detail["companies"] == 3 and detail["written"] == 4 and list(detail["errors"]) == ["MSFT"]


def test_news_rotates_through_the_whole_allow_list():
    names = [f"T{i:02d}" for i in range(23)]
    days = [date(2026, 11, 1).toordinal() + i for i in range(3)]
    picked = [pls.news_tickers(date.fromordinal(d), names) for d in days]
    assert all(len(p) == pls.NEWS_TICKERS_PER_DAY for p in picked)
    assert picked[0] != picked[1]
    assert set().union(*picked) == set(names)
    assert pls.news_tickers(date(2026, 11, 1), ["A", "B"]) == ["A", "B"]


# ── the daily batch ────────────────────────────────────────────────────────────────────────────────────────────────

def _alpaca(account=None, error=None):
    class _Client:
        async def get_account(self):
            if error:
                raise error
            return account

        async def close(self):
            return None
    return _Client


@pytest.mark.asyncio
async def test_daily_batch_records_the_day_start_equity_it_reads(monkeypatch, strategy_jobs):
    account = SimpleNamespace(equity=Decimal("20.00"), cash=Decimal("20.00"))
    monkeypatch.setattr(pls, "AlpacaPaperClient", _alpaca(account))
    when = datetime(2026, 11, 24, 13, 0, tzinfo=timezone.utc)

    out = await pls.run_scheduled_daily_batch(now=when)

    assert out["day_start_equity"]["status"] == "ok"
    assert out["paused"] is False
    assert out["signals"] == 0  # reached the batch: before §17.2041 it aborted with no day-start equity
    async with async_session() as db:
        row = (await db.execute(text("SELECT equity, cash FROM pnl"))).one()
    assert (Decimal(str(row[0])), Decimal(str(row[1]))) == (Decimal("20.00"), Decimal("20.00"))


@pytest.mark.asyncio
async def test_daily_batch_without_alpaca_keys_is_unavailable_and_does_not_raise(monkeypatch, strategy_jobs):
    monkeypatch.setattr(pls, "AlpacaPaperClient",
                        _alpaca(error=AlpacaSecretsMissingError("Missing Alpaca paper trading secrets: ALPACA_API_KEY_ID")))

    out = await pls.run_scheduled_daily_batch(now=NOW)

    assert out["day_start_equity"]["status"] == "unavailable"
    assert "ALPACA_API_KEY_ID" in out["day_start_equity"]["detail"]
    assert out["proposals"] == 0


@pytest.mark.asyncio
async def test_a_paused_strategy_proposes_nothing(monkeypatch, strategy_jobs, ask_without_running):
    monkeypatch.setattr(pls, "AlpacaPaperClient", _alpaca(error=AlpacaSecretsMissingError("no keys")))
    batch = AsyncMock(return_value={"signals": 5, "proposals": 2})
    monkeypatch.setattr(pls, "run_daily_batch", batch)
    await pls.settle_strategy({"decision": "pause", "paused": True, "reason": "lost 3%"})

    out = await pls.run_scheduled_daily_batch(now=NOW)

    assert out["paused"] is True and out["proposals"] == 0
    batch.assert_not_awaited()


# ── the weekly re-evaluation: a losing backtest pauses and asks ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_strategy_pause_follows_the_operators_answers(strategy_jobs, ask_without_running):
    losing = {"decision": "pause", "paused": True, "reason": "lost 3%"}
    winning = {"decision": "continue", "paused": False, "reason": "made 2%"}

    assert await pls.strategy_pause() is None
    assert (await pls.settle_strategy(winning))["asked"] is None       # trading, backtest positive: nothing to ask

    first = (await pls.settle_strategy(losing))["asked"]
    assert first and (await pls.strategy_pause())["job_id"] == first   # paused while the question is open
    assert (await pls.settle_strategy(losing)) == {"asked": None, "open": first}  # never asked twice

    await _answer(first, pls.KEEP_PAUSED)
    assert (await pls.strategy_pause())["reason"].startswith("the operator chose")
    assert (await pls.settle_strategy(losing))["asked"] is None        # kept paused, still losing: nothing new

    second = (await pls.settle_strategy(winning))["asked"]             # positive again: ask to resume
    assert second and second != first
    await _answer(second, pls.RESUME)
    assert await pls.strategy_pause() is None


@pytest.mark.asyncio
async def test_weekly_without_the_sec_contact_still_re_evaluates(monkeypatch, no_sec_email, strategy_jobs,
                                                                ask_without_running):
    monkeypatch.setattr(pls, "re_evaluate_strategy",
                        AsyncMock(return_value={"decision": "pause", "paused": True, "reason": "lost"}))

    report = await pls.run_weekly()

    assert report["13f"]["status"] == "unavailable"
    assert report["re_evaluation"]["status"] == "ok"
    assert report["re_evaluation"]["detail"]["asked"]
    assert await pls.strategy_pause() is not None


# ── the end-of-day snapshot ────────────────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_eod_snapshot_writes_the_accounts_value_then_the_metrics(monkeypatch):
    account = SimpleNamespace(equity=Decimal("25.00"), cash=Decimal("5.00"))
    monkeypatch.setattr(pls, "AlpacaPaperClient", _alpaca(account))

    out = await pls.run_eod_snapshot()

    assert out["status"] == "ok"
    async with async_session() as db:
        rows = (await db.execute(text(
            "SELECT account_value, cash, equity, cumulative_return FROM pnl ORDER BY id"))).all()
    assert len(rows) == 2  # the P&L row, then the metrics row
    assert [(Decimal(str(r[0])), Decimal(str(r[1]))) for r in rows] == [(Decimal("25.00"), Decimal("5.00"))] * 2
    assert rows[1][3] is not None


@pytest.mark.asyncio
async def test_eod_snapshot_without_keys_writes_nothing(monkeypatch):
    monkeypatch.setattr(pls, "AlpacaPaperClient", _alpaca(error=AlpacaSecretsMissingError("no keys")))

    out = await pls.run_eod_snapshot()

    assert out["status"] == "unavailable"
    async with async_session() as db:
        assert (await db.execute(text("SELECT COUNT(*) FROM pnl"))).scalar_one() == 0


# ── the approved order is placed on the answer ─────────────────────────────────────────────────────────────────────

async def _proposal() -> int:
    async with async_session() as db:
        item = (await db.execute(text(
            "INSERT INTO ingested_item (source, external_id, published_ts, payload) "
            "VALUES ('news', 'u1', now(), CAST(:p AS json)) RETURNING id"), {"p": json.dumps({})})).scalar_one()
        signal = (await db.execute(text(
            "INSERT INTO signal (ingested_item_id, ticker, signal_type, direction, rationale) "
            "VALUES (:i, 'AAPL', 'news_context', 'long', 'r') RETURNING id"), {"i": item})).scalar_one()
        pid = (await db.execute(text(
            "INSERT INTO proposal (signal_id, ticker, side, quantity, order_type, rationale) "
            "VALUES (:s, 'AAPL', 'buy', 1, 'market', 'r') RETURNING id"), {"s": signal})).scalar_one()
        await db.commit()
    return int(pid)


@pytest.mark.asyncio
async def test_an_approved_proposal_is_sent_with_its_approval(monkeypatch):
    pid = await _proposal()
    placed = AsyncMock(return_value={"broker_order_id": 7, "status": "submitted"})
    monkeypatch.setattr(gate, "execute_order", placed)

    await gate.handle_approval_answer("job-1", f"approve_proposal_{pid}", {"approved": True, "choice": "Approve"},
                                      {"proposal_id": pid, "proposal": {"proposal_id": pid}})

    async with async_session() as db:
        approval_id = (await db.execute(text("SELECT id FROM approval WHERE proposal_id = :p"), {"p": pid})).scalar_one()
    placed.assert_awaited_once_with({"id": approval_id, "proposal_id": pid, "decision": "approved"})


@pytest.mark.asyncio
async def test_a_rejected_proposal_is_never_sent(monkeypatch):
    pid = await _proposal()
    placed = AsyncMock()
    monkeypatch.setattr(gate, "execute_order", placed)

    await gate.handle_approval_answer("job-1", f"approve_proposal_{pid}", {"approved": False, "note": "no"},
                                      {"proposal_id": pid, "proposal": {"proposal_id": pid}})

    placed.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_order_that_cannot_be_placed_leaves_the_approval_for_execution(monkeypatch):
    pid = await _proposal()
    monkeypatch.setattr(gate, "execute_order", AsyncMock(side_effect=AlpacaSecretsMissingError("no keys")))

    await gate.handle_approval_answer("job-1", f"approve_proposal_{pid}", {"approved": True, "choice": "Approve"},
                                      {"proposal_id": pid, "proposal": {"proposal_id": pid}})

    async with async_session() as db:
        decision = (await db.execute(text("SELECT decision FROM approval WHERE proposal_id = :p"),
                                     {"p": pid})).scalar_one()
        sent = (await db.execute(text("SELECT COUNT(*) FROM broker_order_id"))).scalar_one()
    assert decision == "approved" and sent == 0


# ── 13F: EDGAR's older pages are named, not linked ─────────────────────────────────────────────────────────────────

def _columns(*rows):
    """EDGAR's layout: one array per field, one index per filing."""
    keys = ("accessionNumber", "form", "filingDate", "primaryDocument")
    return {k: [r[i] for r in rows] for i, k in enumerate(keys)}


@pytest.mark.asyncio
async def test_13f_reads_edgars_columns_and_follows_its_named_pages(monkeypatch):
    monkeypatch.setattr(thirteen_f.asyncio, "sleep", AsyncMock())
    older = _columns(("0000950123-24-008740", "13F-HR", "2024-05-15", "primary_doc.xml"),
                     ("0000950123-24-000001", "4", "2024-01-02", "form4.xml"))
    client = FakeClient({"https://data.sec.gov/submissions/": FakeResponse(json_body=older),
                         "https://www.sec.gov/Archives/": FakeResponse(json_body={})})
    initial = {"filings": {
        "recent": _columns(("0000950123-26-011111", "13F-HR", "2026-11-14", "primary_doc.xml"),
                           ("0000950123-26-022222", "SC 13G", "2026-11-10", "doc.xml")),
        "files": [{"name": "CIK0001067983-submissions-001.json"},
                  {"name": "https://www.sec.gov/Archives/older.json"}]}}

    rows = await thirteen_f._fetch_additional_rows("0001067983", client, {}, initial)

    assert client.urls == ["https://data.sec.gov/submissions/CIK0001067983-submissions-001.json",
                           "https://www.sec.gov/Archives/older.json"]
    assert [r["accessionNumber"] for r in rows] == ["0000950123-26-011111", "0000950123-24-008740"]
