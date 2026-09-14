"""§17.1075 — run `alembic upgrade head` at startup, after the SQL runner.

Programmatic, in a worker thread (alembic's command API is sync; env.py
opens its own async engine on that thread's loop). Fail-soft with a loud
log line, like the SQL runner: a broken revision must not take the API
down, it must be visible in /health's migration state.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

logger = logging.getLogger("scaffold.migrations")

_INI = Path("/code/alembic.ini")


def _upgrade_sync() -> dict:
    from alembic import command
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    cfg = Config(str(_INI))
    cfg.set_main_option("script_location", str(_INI.parent / "alembic"))
    heads = ScriptDirectory.from_config(cfg).get_heads()
    command.upgrade(cfg, "head")   # env.py opens its own async engine on this thread
    return {"status": "ok", "heads": heads}


async def _current_revision() -> str | None:
    from alembic.runtime.migration import MigrationContext
    from app.database import engine
    async with engine.connect() as conn:
        return await conn.run_sync(lambda c: MigrationContext.configure(c).get_current_revision())


async def run_alembic_upgrade() -> dict:
    if not _INI.exists():
        return {"status": "skipped", "reason": "no alembic.ini in the image"}
    try:
        before = await _current_revision()
        res = await asyncio.to_thread(_upgrade_sync)
        after = await _current_revision()
        res.update({"before": before, "after": after})
        if before != after:
            logger.info("alembic_upgraded_at_startup: %s -> %s", before, after)
        else:
            logger.info("alembic_current: %s (heads=%s)", after, res["heads"])
        return res
    except Exception as exc:  # noqa: BLE001 — visible, never fatal
        logger.error("alembic_upgrade_failed_at_startup: %r", exc)
        return {"status": "error", "error": repr(exc)}
