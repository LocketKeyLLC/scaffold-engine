r"""§17.2020 — a job blocked on its runner gets its watcher back after an engine restart.

§17.2019's `resume_when_runner_answers` was an in-process task, so every deploy (and every crash) dropped it: the job
stayed `blocked` although the warning had told the operator the engine would start it again by itself. The gate now
stamps the job (`metadata.runner_wait_since`, in the transaction that blocks it, so it equals `updated_at`), and the
lifespan restores a watcher for every job still blocked and untouched since, for what is left of its window.
"""
from __future__ import annotations

import inspect
import pathlib
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import execution_agent as ea
from app.modules import supervised_runs as sr

_JOB = "0fb4ea7f-6d24-4b70-b3db-a3ea244c7a4d"


class _Db:
    def __init__(self, rows=None, boom=None):
        self.rows, self.boom, self.sql = rows or [], boom, []

    def __call__(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, params=None):
        if self.boom:
            raise self.boom
        self.sql.append((str(stmt), params))
        r = MagicMock()
        r.all.return_value = self.rows
        return r


@pytest.mark.asyncio
async def test_startup_restores_a_watcher_for_what_is_left_of_the_window(monkeypatch):
    import app.database as dbm
    db = _Db(rows=[(_JOB, 600.0)])
    monkeypatch.setattr(dbm, "async_session", db)
    resume = MagicMock()
    monkeypatch.setattr(sr, "resume_when_runner_answers", resume)
    assert await sr.resume_waiting_jobs_on_startup() == [_JOB]
    resume.assert_called_once_with(_JOB, window=sr.RESUME_WINDOW_SECONDS - 600.0)
    sql = db.sql[0][0]
    # only a job still blocked, untouched since the stamp, and inside the window
    assert "status = 'blocked'" in sql
    assert "(metadata->>'runner_wait_since')::timestamptz = updated_at" in sql
    assert db.sql[0][1] == {"window": sr.RESUME_WINDOW_SECONDS}


@pytest.mark.asyncio
async def test_startup_never_stops_on_it(monkeypatch):
    import app.database as dbm
    monkeypatch.setattr(dbm, "async_session", _Db(boom=RuntimeError("db gone")))
    resume = MagicMock()
    monkeypatch.setattr(sr, "resume_when_runner_answers", resume)
    assert await sr.resume_waiting_jobs_on_startup() == []
    assert resume.call_count == 0


@pytest.mark.asyncio
async def test_a_restored_watcher_keeps_to_its_remaining_window(monkeypatch):
    """window=0: the time ran out while the engine was down -- it stops without reading the runner or starting."""
    import app.database as dbm
    import app.modules.run_broker as rb

    class _Stamp(_Db):
        async def execute(self, stmt, params=None):
            r = MagicMock()
            r.scalar.return_value = "t0"
            return r
    monkeypatch.setattr(dbm, "async_session", _Stamp())
    cs = AsyncMock(return_value=("open", "pve-runner"))
    monkeypatch.setattr(sr, "channel_state", cs)
    start = MagicMock()
    monkeypatch.setattr(rb, "start", start)
    sr.resume_when_runner_answers(_JOB, window=0.0)
    await sr._resuming[_JOB]
    assert cs.await_count == 0 and start.call_count == 0


@pytest.mark.asyncio
async def test_the_stamp_is_written_with_the_database_clock():
    db = _Db()
    await sr.mark_waiting_for_runner(db, _JOB)
    sql, params = db.sql[0]
    assert "jsonb_build_object('runner_wait_since', NOW())" in sql and params == {"id": _JOB}
    assert "updated_at" not in sql          # the trigger writes it, with the same NOW()


def test_the_gate_stamps_the_job_in_the_transaction_that_blocks_it():
    src = inspect.getsource(ea)
    gate = src[src.index("§17.624 — hands-on assist gate (A)"):]
    t = gate.index('reason="hands_on_gate:runner_unreachable")')
    stamp = gate.index("await _sr_chan.mark_waiting_for_runner(db, job_id)")
    assert t < stamp < gate.index("await db.commit()", t)


def test_the_lifespan_restores_the_watchers_after_settling_interrupted_runs():
    from app import main
    src = pathlib.Path(main.__file__).read_text(encoding="utf-8")
    assert src.index("await settle_interrupted_runs()") < src.index("await resume_waiting_jobs_on_startup()")
