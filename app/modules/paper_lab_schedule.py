# app/modules/paper_lab_schedule.py
"""§17.2041 — the paper lab's recurring runs: what the scheduler starts, in the order the lab's design reads.

Live (§17.2000, main's tree): 10 of the lab's 28 delivered modules were imported by nothing. The six ingesters never
ran, so the point-in-time store the daily signal batch reads stayed empty; the weekly re-evaluation never ran, so a
losing strategy was never paused; nothing recorded the day-start equity the batch needs (every run aborted "No
day-start equity recorded"); an approved proposal waited for a manual POST; and no snapshot ever wrote the P&L and
metrics rows the dashboard draws. The lab's own answers (job 2772acae): a daily batch before the US open; Form 4
weighted heaviest, then 13F and Congress, with FINRA, news and Reddit as context; approve every order; a losing
backtest pauses the strategy and asks.

Runs (registered by `paper_wire_engine_entry_module.install`, on the engine's one APScheduler):

  - `run_daily_ingestion` (weekdays 11:00 UTC, before the 13:00 batch): Form 4 for the allow-list's companies,
    congressional disclosures, FINRA daily short volume, news for a rotating slice of the allow-list, Reddit.
  - `run_scheduled_daily_batch` (the existing 13:00 job): records the day-start equity from the paper account, then
    proposes unless the strategy is paused.
  - `run_eod_snapshot` (weekdays 21:30 UTC, after the close): per-position P&L with the account's own cash and
    equity, then the performance metrics row.
  - `run_weekly` (Saturdays 12:00 UTC): 13F holdings (filed quarterly) and the strategy re-evaluation.

Every part needing a secret the operator has not stored is reported ``unavailable`` with the secret's name and the
run goes on: one source never stops the others, and nothing here raises to the scheduler."""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Optional

import httpx
from sqlalchemy import text

from app.database import async_session
from app.modules.operator_ask import answer_handler, answer_of, decision, open_decision_job
from app.modules.paper_13f_holdings_ingestion import ingest_13f_holdings
from app.modules.paper_alpaca_paper_client import AlpacaPaperClient, AlpacaSecretsMissingError
from app.modules.paper_congressional_trade_ingestion import fetch_disclosures
from app.modules.paper_daily_signal_proposal_batch import run_daily_batch
from app.modules.paper_day_start_equity_recording import record_day_start_equity
from app.modules.paper_edgar_form_4_ingestion import SEC_USER_AGENT_SECRET, ingest_form_4_filings
from app.modules.paper_finra_data_ingestion import ingest_finra_files
from app.modules.paper_news_ingestion import ingest_news_batch
from app.modules.paper_p_l_calculator import calculate_pnl
from app.modules.paper_performance_metrics_calculator import get_performance_metrics
from app.modules.paper_reddit_ingestion import RedditSecretsMissingError, ingest_reddit_posts
from app.modules.paper_risk_control_module import ALLOW_LIST
from app.modules.paper_weekly_strategy_re_evaluation import re_evaluate_strategy
from app.modules.runner_secrets import values_for

logger = logging.getLogger("scaffold")

SCHEDULE_MODULE = "app.modules.paper_lab_schedule"

#: the scheduler's job ids and times (UTC), next to the existing `paper_daily_batch` at 13:00
DAILY_INGEST_JOB_ID = "paper_daily_ingest"
DAILY_INGEST_TRIGGER = {"trigger": "cron", "day_of_week": "mon-fri", "hour": 11, "minute": 0}
EOD_SNAPSHOT_JOB_ID = "paper_eod_snapshot"
EOD_SNAPSHOT_TRIGGER = {"trigger": "cron", "day_of_week": "mon-fri", "hour": 21, "minute": 30}
WEEKLY_JOB_ID = "paper_weekly"
WEEKLY_TRIGGER = {"trigger": "cron", "day_of_week": "sat", "hour": 12, "minute": 0}

#: SEC's ticker -> CIK list (the Form 4 ingester takes a CIK); fetched with the SEC user agent
SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_USER_AGENT_TEMPLATE = "PaperTradingScaffold Research {email}"
#: 13F filers watched (the brief: "watch other AI/people"); a filer's CIK, zero-padded as EDGAR's submissions URL wants
THIRTEEN_F_FILERS: dict[str, str] = {"Berkshire Hathaway": "0001067983"}
#: bounded per run: SEC asks for <= 10 requests/s and the ingesters sleep 0.5 s per request
FORM4_FILINGS_PER_COMPANY = 5
#: news searches per run, rotating through the allow-list (SearXNG's engines rate-limit a burst)
NEWS_TICKERS_PER_DAY = 10
NEWS_RESULTS_PER_TICKER = 10

#: the strategy-pause question (a code-opened decision; the options are the code's)
RESUME, KEEP_PAUSED = "Resume trading", "Keep paused"
STRATEGY_DECISION_KEY = "paper_strategy"
STRATEGY_HANDLER = f"{SCHEDULE_MODULE}:handle_strategy_answer"

OK, UNAVAILABLE, FAILED = "ok", "unavailable", "failed"


def _status(state: str, detail: Any) -> dict[str, Any]:
    return {"status": state, "detail": detail}


async def _guarded(name: str, run: Callable[[], Awaitable[Any]]) -> dict[str, Any]:
    """Run one part; a missing secret is ``unavailable``, any other failure ``failed``. Never raises."""
    try:
        out = _status(OK, await run())
    except (RedditSecretsMissingError, AlpacaSecretsMissingError) as exc:
        out = _status(UNAVAILABLE, str(exc))
    except Exception as exc:  # noqa: BLE001 - one source never stops the others
        logger.warning('event="paper_lab_part_failed" part=%s error=%r', name, exc)
        out = _status(FAILED, repr(exc))
    logger.info('event="paper_lab_part" part=%s status=%s', name, out["status"])
    return out


def _previous_weekday(day: date) -> date:
    day -= timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def news_tickers(day: date, tickers: Optional[list[str]] = None) -> list[str]:
    """The allow-list slice searched on ``day``: NEWS_TICKERS_PER_DAY names, moving on each day, every name in turn."""
    names = sorted(tickers if tickers is not None else ALLOW_LIST)
    if len(names) <= NEWS_TICKERS_PER_DAY:
        return names
    start = (day.toordinal() * NEWS_TICKERS_PER_DAY) % len(names)
    return [names[(start + i) % len(names)] for i in range(NEWS_TICKERS_PER_DAY)]


async def _sec_contact_email() -> Optional[str]:
    async with async_session() as db:
        return (await values_for(db, [SEC_USER_AGENT_SECRET])).get(SEC_USER_AGENT_SECRET) or None


def _sec_unavailable() -> dict[str, Any]:
    return _status(UNAVAILABLE, f"the operator secret {SEC_USER_AGENT_SECRET} is not stored yet "
                                "(SEC asks every request to name a contact)")


async def _company_ciks(client: Any, headers: dict[str, str], tickers: list[str]) -> dict[str, int]:
    """``{ticker: cik}`` for the allow-list names SEC lists (an ETF trust may not file Form 4 at all)."""
    response = await client.get(SEC_COMPANY_TICKERS_URL, headers=headers)
    response.raise_for_status()
    by_ticker = {str(row.get("ticker", "")).upper(): int(row["cik_str"])
                 for row in (response.json() or {}).values() if isinstance(row, dict) and row.get("cik_str")}
    out: dict[str, int] = {}
    for ticker in tickers:
        cik = by_ticker.get(ticker.upper()) or by_ticker.get(ticker.upper().replace(".", "-"))
        if cik:
            out[ticker] = cik
    return out


async def _ingest_form_4(client: Any, email: str) -> dict[str, Any]:
    headers = {"User-Agent": SEC_USER_AGENT_TEMPLATE.format(email=email)}
    ciks = await _company_ciks(client, headers, sorted(ALLOW_LIST))
    written: dict[str, int] = {}
    errors: dict[str, str] = {}
    for ticker, cik in ciks.items():
        try:
            async with async_session() as db:
                written[ticker] = await ingest_form_4_filings(
                    db, cik, client=client, contact_email=email, max_filings=FORM4_FILINGS_PER_COMPANY)
                await db.commit()
        except Exception as exc:  # noqa: BLE001 - one company never stops the rest
            errors[ticker] = repr(exc)
    return {"companies": len(ciks), "written": sum(written.values()), "errors": errors}


async def run_daily_ingestion(*, now: Optional[datetime] = None) -> dict[str, Any]:
    """Fill the point-in-time store before the daily batch reads it. Returns one status per source."""
    now = now or datetime.now(timezone.utc)
    report: dict[str, Any] = {}
    email = await _sec_contact_email()
    async with httpx.AsyncClient(timeout=30.0) as client:
        report["form_4"] = (await _guarded("form_4", lambda: _ingest_form_4(client, email))
                            if email else _sec_unavailable())

        async def _congress() -> dict[str, Any]:
            got = await fetch_disclosures(client)
            return {"stored": len(got.rows), "errors": got.errors}
        report["congress"] = await _guarded("congress", _congress)

        report["finra"] = await _guarded("finra", lambda: ingest_finra_files(
            source="short_interest", release_dates=[_previous_weekday(now.date())], http_get=client.get))

    async def _news() -> dict[str, int]:
        got = await ingest_news_batch(news_tickers(now.date()), max_results=NEWS_RESULTS_PER_TICKER)
        return {ticker: len(items) for ticker, items in got.items()}
    report["news"] = await _guarded("news", _news)
    report["reddit"] = await _guarded("reddit", ingest_reddit_posts)
    logger.info('event="paper_daily_ingestion" %s',
                " ".join(f"{k}={v['status']}" for k, v in report.items()))
    return report


async def _record_day_start(now: datetime) -> dict[str, Any]:
    client = AlpacaPaperClient()
    try:
        account = await client.get_account()
    finally:
        await client.close()
    async with async_session() as db:
        row_id = await record_day_start_equity(
            db=db, equity=account.equity, cash=account.cash, account_value=account.equity, as_of_ts=now)
    return {"pnl_id": row_id, "equity": str(account.equity)}


async def run_scheduled_daily_batch(*, now: Optional[datetime] = None) -> dict[str, Any]:
    """The 13:00 run: the day-start equity first (the batch reads it), then the batch -- unless the strategy is paused."""
    now = now or datetime.now(timezone.utc)
    equity = await _guarded("day_start_equity", lambda: _record_day_start(now))
    paused = await strategy_pause()
    if paused:
        logger.info('event="paper_daily_batch_paused" job=%s', paused["job_id"])
        return {"day_start_equity": equity, "paused": True, "reason": paused["reason"], "proposals": 0}
    batch = await run_daily_batch(timestamp=now)
    return {"day_start_equity": equity, "paused": False, **batch}


async def _snapshot() -> dict[str, Any]:
    client = AlpacaPaperClient()
    try:
        account = await client.get_account()
    finally:
        await client.close()
    positions = await calculate_pnl(account_cash=account.cash, account_equity=account.equity)
    metrics = await get_performance_metrics()
    return {"positions": len(positions), "account_value": str(account.equity),
            "return_after_costs": str(metrics.get("return_after_costs"))}


async def run_eod_snapshot() -> dict[str, Any]:
    """After the close: the account's P&L row, then the metrics row the dashboard draws."""
    return await _guarded("eod_snapshot", _snapshot)


# ── the strategy pause: a losing backtest pauses and asks ─────────────────────────────────────────────────────────

async def _latest_strategy_question(db: Any) -> Optional[str]:
    row = (await db.execute(text(
        "SELECT id FROM jobs WHERE metadata->>'on_answer' = :h ORDER BY created_at DESC LIMIT 1"),
        {"h": STRATEGY_HANDLER})).first()
    return str(row[0]) if row else None


async def strategy_pause(db: Optional[Any] = None) -> Optional[dict[str, Any]]:
    """``{"job_id", "reason"}`` while the strategy is paused, else None. Paused = the latest strategy question is
    still open, or the operator answered it "Keep paused"; answered "Resume trading" (or never asked) = trading."""
    if db is None:
        async with async_session() as own:
            return await strategy_pause(own)
    job_id = await _latest_strategy_question(db)
    if job_id is None:
        return None
    answer = await answer_of(db, job_id, STRATEGY_DECISION_KEY)
    if answer is None:
        return {"job_id": job_id, "reason": "the weekly re-evaluation's question is waiting for the operator"}
    if answer.get("choice") == KEEP_PAUSED:
        return {"job_id": job_id, "reason": "the operator chose to keep the strategy paused"}
    return None


async def _ask(db: Any, verdict: dict[str, Any], question: str) -> str:
    detail = (f"{verdict.get('reason', '')}\n\nBacktest window: {verdict.get('start_date')} to "
              f"{verdict.get('end_date')}. While this question is open the daily batch proposes nothing.")
    return await open_decision_job(
        db, title="Paper lab: weekly strategy re-evaluation",
        decisions=[decision(STRATEGY_DECISION_KEY, question, detail, options=[RESUME, KEEP_PAUSED],
                            payload={"total_return": verdict.get("total_return"),
                                     "decision": verdict.get("decision")})],
        owner="operator", on_answer=STRATEGY_HANDLER)


async def settle_strategy(verdict: dict[str, Any], db: Optional[Any] = None) -> dict[str, Any]:
    """Act on a weekly verdict. A losing backtest while trading pauses and asks; a winning one while the operator
    kept it paused asks whether to resume. One question at a time: an open one is never asked again."""
    if db is None:
        async with async_session() as own:
            return await settle_strategy(verdict, own)
    job_id = await _latest_strategy_question(db)
    answer = await answer_of(db, job_id, STRATEGY_DECISION_KEY) if job_id else {"choice": RESUME}
    if answer is None:
        return {"asked": None, "open": job_id}
    paused = answer.get("choice") == KEEP_PAUSED
    if verdict.get("paused") and not paused:
        return {"asked": await _ask(db, verdict, "The weekly backtest loses money: resume trading or keep the "
                                                 "strategy paused?")}
    if not verdict.get("paused") and paused:
        return {"asked": await _ask(db, verdict, "The weekly backtest is positive again: resume trading?")}
    return {"asked": None}


@answer_handler
async def handle_strategy_answer(job_id: str, node_key: str, answer: dict[str, Any],
                                 payload: dict[str, Any]) -> None:
    """The answer is the state (`strategy_pause` reads it); this records that it came."""
    logger.info('event="paper_strategy_answered" job=%s choice=%s', job_id, answer.get("choice"))


async def run_weekly() -> dict[str, Any]:
    """Saturdays: the quarter's 13F holdings, then the strategy re-evaluation and what it decides."""
    report: dict[str, Any] = {}
    email = await _sec_contact_email()
    if email:
        async def _thirteen_f() -> dict[str, int]:
            out: dict[str, int] = {}
            async with httpx.AsyncClient(timeout=30.0) as client:
                for name, cik in THIRTEEN_F_FILERS.items():
                    async with async_session() as db:
                        out[name] = await ingest_13f_holdings(cik, db=db, client=client)
            return out
        report["13f"] = await _guarded("13f", _thirteen_f)
    else:
        report["13f"] = _sec_unavailable()

    async def _re_evaluate() -> dict[str, Any]:
        verdict = await re_evaluate_strategy()
        return {"decision": verdict.get("decision"), "reason": verdict.get("reason"),
                **await settle_strategy(verdict)}
    report["re_evaluation"] = await _guarded("re_evaluation", _re_evaluate)
    return report


__all__ = [
    "run_daily_ingestion",
    "run_eod_snapshot",
    "run_scheduled_daily_batch",
    "run_weekly",
    "settle_strategy",
    "strategy_pause",
]
