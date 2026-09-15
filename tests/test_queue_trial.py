"""§17.1076 — the procrastinate trial: task registered, off by default, loop delegated only when on."""
import pathlib

from app.config import settings

ROOT = pathlib.Path(__file__).resolve().parents[1]


def test_queue_is_off_by_default_and_the_sweep_task_is_registered():
    assert settings.queue_enabled is False
    from app.queue import app, cleanup_sweep
    assert "scaffold.cleanup_sweep" in app.tasks
    assert app.tasks["scaffold.cleanup_sweep"].queue == "maintenance"
    assert cleanup_sweep.lock == "cleanup_sweep"


def test_conninfo_is_a_plain_postgres_dsn():
    from app.queue import _conninfo
    assert _conninfo().startswith("postgresql://") and "asyncpg" not in _conninfo()


def test_startup_delegates_the_loop_only_when_the_queue_owns_it():
    src = (ROOT / "app" / "main.py").read_text(encoding="utf-8")
    block = src[src.index("if settings.queue_enabled:"):src.index("_cleanup_task = start_cleanup_task()") + 40]
    assert "cleanup_loop_delegated_to_queue" in block and "_cleanup_task = None" in block
    assert "if _cleanup_task is not None:\n        _cleanup_task.cancel()" in src


# ── §17.1078 — the follow-ons: interval jobs + zombie sweep on the queue ──

def test_follow_on_tasks_are_registered_with_locks_and_queues():
    from app.queue import app
    for name, queue in (("scaffold.threshold_eval", "maintenance"), ("scaffold.calibration_watchdog", "maintenance"),
                        ("scaffold.model_role_learning", "learning"), ("scaffold.zombie_run_sweep", "maintenance")):
        assert name in app.tasks, name
        assert app.tasks[name].queue == queue and app.tasks[name].lock == name.split(".", 1)[1]


def test_cron_from_seconds_rounds_down_to_a_divisor_and_never_slower():
    from app.queue import cron_from_seconds as c
    assert c(300) == "*/5 * * * *" and c(900) == "*/15 * * * *" and c(60) == "*/1 * * * *"
    assert c(7 * 60) == "*/6 * * * *"            # 7 does not divide 60 → 6 (faster, never slower)
    assert c(3600) == "0 */1 * * *" and c(5 * 3600) == "0 */4 * * *"
    assert c(7 * 86400) == "0 0 */7 * *" and c(90 * 86400) == "0 0 */28 * *"
    assert c(10) == "*/1 * * * *"


import pytest


@pytest.mark.asyncio
async def test_follow_on_tasks_honour_their_feature_valves(monkeypatch):
    """The queue owns WHEN; the feature valve still owns WHETHER."""
    from unittest.mock import AsyncMock
    from app import queue as q
    monkeypatch.setattr(settings, "alert_eval_enabled", False)
    monkeypatch.setattr(settings, "calibration_watchdog_enabled", False)
    monkeypatch.setattr(settings, "model_role_learning_enabled", False)
    assert (await q.threshold_eval(1))["skipped"] == "alert_eval_enabled=false"
    assert (await q.calibration_watchdog(1))["skipped"] == "calibration_watchdog_enabled=false"
    assert (await q.model_role_learning(1))["skipped"] == "model_role_learning_enabled=false"
    monkeypatch.setattr(settings, "alert_eval_enabled", True)
    tick = AsyncMock()
    from app.observability import thresholds
    monkeypatch.setattr(thresholds, "tick", tick)
    assert (await q.threshold_eval(2)) == {"ran": "threshold_eval", "ts": 2} and tick.await_count == 1


@pytest.mark.asyncio
async def test_zombie_sweep_task_passes_the_age_cap_and_the_sweep_scopes_its_sql(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock
    from app import queue as q
    from app.modules import assist_turn
    real_sweep = assist_turn.sweep_zombie_runs
    seen = {}
    async def fake_sweep(*, older_than_minutes=None):
        seen["mins"] = older_than_minutes; return 3
    monkeypatch.setattr(assist_turn, "sweep_zombie_runs", fake_sweep)
    assert (await q.zombie_run_sweep(9)) == {"marked": 3, "ts": 9} and seen["mins"] == settings.queue_zombie_run_max_age_minutes
    # the real sweep: startup form has no age clause; the queue form adds it and changes the wording
    executed = []
    class _Res: rowcount = 2
    class _DB:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def execute(self, stmt, params): executed.append((str(stmt), params)); return _Res()
        async def commit(self): pass
    import app.database as database
    monkeypatch.setattr(database, "async_session", lambda: _DB())
    assert await real_sweep() == 2
    assert await real_sweep(older_than_minutes=30) == 2
    sql0, p0 = executed[0]; sql1, p1 = executed[1]
    assert "created_at" not in sql0 and "mins" not in p0 and "restarted mid-turn" in p0["f"]
    assert "created_at < now() - make_interval(mins => :mins)" in sql1 and p1["mins"] == 30
    assert "stalled for more than 30 minutes" in p1["f"] and "restarted" not in p1["f"]


def test_scheduler_delegates_the_three_interval_jobs_when_the_queue_owns_them():
    src = (ROOT / "app" / "scheduler.py").read_text(encoding="utf-8")
    block = src[src.index("def _register_observability_jobs"):src.index("async def shutdown_scheduler")]
    assert block.index("if settings.queue_enabled:") < block.index("if settings.alert_eval_enabled:")
    assert "observability_jobs_delegated_to_queue" in block


@pytest.mark.asyncio
async def test_learning_task_skips_when_it_succeeded_within_the_interval(monkeypatch):
    """Live §17.1078: the first one-shot worker deferred the weekly learning
    tick at startup and began a real golden re-A/B. The task must consult its
    own success history before spending model calls."""
    from unittest.mock import AsyncMock
    from app import queue as q
    monkeypatch.setattr(settings, "model_role_learning_enabled", True)
    calls = []
    async def within(task, seconds):
        calls.append((task, seconds)); return 120.0
    monkeypatch.setattr(q, "_succeeded_within", within)
    from app.modules import model_role_learning as mrl
    tick = AsyncMock(); monkeypatch.setattr(mrl, "tick", tick)
    out = await q.model_role_learning(3)
    assert out["skipped"].startswith("last success 120s ago") and tick.await_count == 0
    assert calls == [("scaffold.model_role_learning", int(settings.model_role_learning_interval_seconds * 0.9))]
    async def never(task, seconds): return None
    monkeypatch.setattr(q, "_succeeded_within", never)
    assert (await q.model_role_learning(4)) == {"ran": "model_role_learning", "ts": 4} and tick.await_count == 1
