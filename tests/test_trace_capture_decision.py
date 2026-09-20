"""§17.1140 (ledger O-1) — the trace-capture decision, made concrete.

Capture is ON in the deployment (.env) with a retention sweep so the table
cannot grow without bound, and assist turns attribute their model calls to
the session's job so a captured assist prompt is reachable from the job's
Traces tab. These pin the pieces that are not covered elsewhere.
"""
from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_retention_default_is_bounded_and_documented():
    from app.config import Settings
    f = Settings.model_fields["trace_retention_days"]
    assert f.default == 14
    ex = (ROOT / ".env.example").read_text(encoding="utf-8")
    for var in ("TRACE_CAPTURE_ENABLED", "TRACE_CAPTURE_MAX_CHARS", "TRACE_RETENTION_DAYS"):
        assert re.search(rf"^# {var}=", ex, re.M), f"{var} undocumented in .env.example"


@pytest.mark.asyncio
async def test_sweep_deletes_rows_older_than_the_retention(monkeypatch):
    from app.config import settings
    from app.modules import cleanup
    monkeypatch.setattr(settings, "trace_retention_days", 14)
    res = MagicMock(); res.rowcount = 37
    db = AsyncMock(); db.execute = AsyncMock(return_value=res); db.commit = AsyncMock()
    assert await cleanup.sweep_llm_traces(db) == 37
    sql = str(db.execute.await_args.args[0]); params = db.execute.await_args.args[1]
    assert "DELETE FROM llm_traces" in sql and "make_interval(days => :d)" in sql and params == {"d": 14}
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_sweep_is_a_noop_when_retention_is_zero(monkeypatch):
    from app.config import settings
    from app.modules import cleanup
    monkeypatch.setattr(settings, "trace_retention_days", 0)
    db = AsyncMock(); db.execute = AsyncMock()
    assert await cleanup.sweep_llm_traces(db) == 0
    db.execute.assert_not_awaited()


def test_the_cleanup_cycle_runs_the_trace_sweep_fail_soft():
    src = (ROOT / "app" / "modules" / "cleanup.py").read_text(encoding="utf-8")
    body = src[src.index("async def _run_once("):]
    assert "sweep_llm_traces(db)" in body
    assert "llm_traces_sweep_failed" in body, "the sweep must never abort the reaper cycle"


def test_assist_turns_attribute_their_calls_to_the_job():
    """The executor set current_job_id; assist turns did not, so assist call
    logs / traces / cost rollups were job-less (§17.1140)."""
    src = (ROOT / "app" / "modules" / "assist_turn.py").read_text(encoding="utf-8")
    assert "SELECT job_id FROM assist_sessions WHERE id = :sid" in src
    assert "current_job_id.set(str(_jid))" in src
    assert "current_job_id.reset(_job_token)" in src, "the ContextVar must be reset at turn end"
    assert src.index("current_job_id.set(str(_jid))") < src.index("current_job_id.reset(_job_token)")
