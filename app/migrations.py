"""Schema migration runner (#10).

Scans ``db/migrations/*.sql`` and applies any files not yet recorded in the
``schema_migrations`` tracking table. Ordering is lexicographic on filename,
which matches the numeric prefix convention (001_, 002_, ... 013_).

Idempotent: re-runs are no-ops once all files are applied.
Atomic per file: each migration runs in its own transaction.
Pre-seeding: existing deployments have migrations 002–013 applied manually,
so this module seeds those rows on first run against an already-initialized
database to avoid re-applying SQL that would conflict with live schema.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from sqlalchemy import text

from app.database import async_session

logger = logging.getLogger("scaffold.migrations")

# Host-mount expected by docker-compose: ./db:/code/db:ro
_MIGRATIONS_DIR = Path("/code/db/migrations")

# Files assumed already applied on existing deployments (pre-runner baseline).
# On a fresh DB these will run; on an established DB they'll be seeded into
# schema_migrations without re-executing (see _seed_baseline_if_established).
_PRE_RUNNER_BASELINE = frozenset({
    "002_add_confidence.sql",
    "003_add_compiled_output.sql",
    "004_add_job_statuses.sql",
    "005_add_domain_tool.sql",
    "006_add_indexes.sql",
    "007_ideation_workflow.sql",
    "008_add_ideation_statuses.sql",
    "009_dedup_log.sql",
    "010_research_sessions.sql",
    "011_scheduled_jobs.sql",
    "012_research_sessions_state.sql",
    "013_research_pause.sql",
    "014_research_sessions_depth_varchar.sql",
    "015_research_sessions_status_varchar.sql",
    "016_scheduler_timezone.sql",
    "017_dag_nodes_is_output_node.sql",
})

_CREATE_TRACKING_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
"""


async def _ensure_tracking_table(db) -> None:
    await db.execute(text(_CREATE_TRACKING_TABLE))
    await db.commit()


async def _get_applied(db) -> set[str]:
    result = await db.execute(text("SELECT filename FROM schema_migrations"))
    return {row[0] for row in result.fetchall()}


async def _seed_baseline_if_established(db, applied: set[str]) -> None:
    """If this is an existing DB (has 'jobs' table) with no recorded migrations,
    seed the pre-runner baseline as already-applied so we don't re-run them.

    Detection: 'jobs' table exists → established DB → seed baseline.
    """
    if applied:
        return  # already tracked; nothing to seed
    result = await db.execute(text(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_name = 'jobs' AND table_schema = current_schema()"
    ))
    if result.first() is None:
        return  # fresh DB, no seeding needed
    logger.warning(
        "migrations_seed_baseline: established DB detected with no schema_migrations "
        "entries; seeding %d baseline files as already-applied",
        len(_PRE_RUNNER_BASELINE),
    )
    for fname in sorted(_PRE_RUNNER_BASELINE):
        await db.execute(
            text("INSERT INTO schema_migrations (filename) VALUES (:f) "
                 "ON CONFLICT (filename) DO NOTHING"),
            {"f": fname},
        )
    await db.commit()


async def _apply_one(db, path: Path) -> None:
    sql = path.read_text()
    if not sql.strip():
        logger.warning("migration_empty_skipped: file=%s", path.name)
        return
    # Each migration runs in its own transaction.
    await db.execute(text(sql))
    await db.execute(
        text("INSERT INTO schema_migrations (filename) VALUES (:f)"),
        {"f": path.name},
    )
    await db.commit()
    logger.info("migration_applied: file=%s", path.name)


async def run_migrations() -> dict:
    """Apply any unapplied migrations. Returns a summary dict."""
    if not _MIGRATIONS_DIR.exists():
        logger.warning("migrations_dir_missing: path=%s", _MIGRATIONS_DIR)
        return {"status": "skipped", "reason": "dir_missing", "applied": []}

    files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        return {"status": "ok", "applied": []}

    applied_this_run: list[str] = []
    async with async_session() as db:
        try:
            await _ensure_tracking_table(db)
            already_applied = await _get_applied(db)
            await _seed_baseline_if_established(db, already_applied)
            already_applied = await _get_applied(db)  # refresh after seeding

            for path in files:
                if path.name in already_applied:
                    continue
                try:
                    await _apply_one(db, path)
                    applied_this_run.append(path.name)
                except Exception as exc:
                    logger.error(
                        "migration_failed: file=%s error=%s",
                        path.name, exc,
                    )
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                    raise
        except Exception as exc:
            return {"status": "error", "error": str(exc), "applied": applied_this_run}

    logger.info(
        "migrations_complete: applied_count=%d total_files=%d",
        len(applied_this_run), len(files),
    )
    return {"status": "ok", "applied": applied_this_run, "total": len(files)}


if __name__ == "__main__":
    # Manual invocation: `python -m app.migrations`
    result = asyncio.run(run_migrations())
    print(result)
