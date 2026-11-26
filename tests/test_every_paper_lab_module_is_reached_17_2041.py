"""§17.2041 — every module the paper lab delivered is reached from the lab's entry, and the scheduler starts its runs.

Live (§17.2000, main's tree 2026-11-22): `scripts/deliver_job.py`'s `unreached` over main named 10 of the lab's 28
`paper_*` modules: the six ingesters (13F, congressional, EDGAR Form 4, FINRA, news, Reddit), `paper_order_executor`,
`paper_p_l_calculator`, `paper_performance_metrics_calculator` and `paper_weekly_strategy_re_evaluation`. Nothing ran
the ingestion the daily batch reads, nothing paused a losing strategy, and no snapshot wrote the dashboard's metrics."""
from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI

from app.modules.module_reach import reached

REPO = Path(__file__).resolve().parent.parent
MODULES = REPO / "app" / "modules"
ENTRY = "paper_wire_engine_entry_module"
THE_TEN = (
    "paper_13f_holdings_ingestion",
    "paper_congressional_trade_ingestion",
    "paper_edgar_form_4_ingestion",
    "paper_finra_data_ingestion",
    "paper_news_ingestion",
    "paper_reddit_ingestion",
    "paper_order_executor",
    "paper_p_l_calculator",
    "paper_performance_metrics_calculator",
    "paper_weekly_strategy_re_evaluation",
)


@pytest.fixture(scope="module")
def deliver_job():
    spec = importlib.util.spec_from_file_location("deliver_job_2041", REPO / "scripts" / "deliver_job.py")
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def _paper_modules() -> list[str]:
    return sorted(p.stem for p in MODULES.glob("paper_*.py"))


def test_the_delivery_check_finds_no_paper_module_unreached(deliver_job):
    """The check the delivery prints (§17.2000), run over this tree with every `paper_*` module as the job's."""
    mods = _paper_modules()
    assert set(THE_TEN) <= set(mods)
    assert deliver_job.unreached(REPO, mods) == []


def test_each_of_the_ten_is_reached_from_the_labs_own_entry():
    """Not merely from some engine file: from the module `app/labs.py` starts."""
    codes = {p.stem: p.read_text() for p in MODULES.glob("*.py")}
    got = reached(codes, [ENTRY])
    assert [m for m in THE_TEN if m not in got] == []


def test_install_schedules_ingestion_before_the_batch_and_the_other_runs():
    from app.modules import paper_lab_schedule as pls
    from app.modules import paper_wire_engine_entry_module as entry

    scheduler = MagicMock()
    with patch.object(entry, "get_scheduler", return_value=scheduler):
        entry.install(FastAPI())
    jobs = {c.kwargs["id"]: c for c in scheduler.add_job.call_args_list}

    assert jobs[entry.DAILY_BATCH_JOB_ID].args[0] is pls.run_scheduled_daily_batch
    assert jobs[pls.DAILY_INGEST_JOB_ID].args[0] is pls.run_daily_ingestion
    assert jobs[pls.EOD_SNAPSHOT_JOB_ID].args[0] is pls.run_eod_snapshot
    assert jobs[pls.WEEKLY_JOB_ID].args[0] is pls.run_weekly
    ingest = jobs[pls.DAILY_INGEST_JOB_ID].kwargs
    batch = jobs[entry.DAILY_BATCH_JOB_ID].kwargs
    assert (ingest["hour"], ingest["minute"]) < (batch["hour"], batch["minute"])
    assert all(c.kwargs.get("replace_existing") for c in jobs.values())
