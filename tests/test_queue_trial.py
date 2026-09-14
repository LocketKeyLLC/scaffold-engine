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
