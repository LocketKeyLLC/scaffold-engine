"""Schema migration runner (#10).

Scans ``db/migrations/*.sql`` and applies any files not yet recorded in the
``schema_migrations`` tracking table. Ordering is lexicographic on filename.

Guarantees:
- Idempotent: re-runs are no-ops once all files are applied.
- Atomic per file: DDL + tracking INSERT share a single transaction.
- Mutually exclusive across processes: Postgres advisory lock prevents two
  orchestrator replicas racing on startup.
- Pre-seeding: established deployments have migrations 002–017 applied
  manually; baseline detection seeds them as already-applied on first run.
"""
from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

from sqlalchemy import text

from app.database import async_session

logger = logging.getLogger("scaffold.migrations")

# Host-mount expected by docker-compose: ./db:/code/db:ro
_MIGRATIONS_DIR = Path("/code/db/migrations")

# Arbitrary app-scoped key for pg_advisory_xact_lock. Any constant 64-bit int
# works; this one is unique to the migration runner.
_ADVISORY_LOCK_KEY = 817263541

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


async def _get_applied(db) -> set[str]:
    result = await db.execute(text("SELECT filename FROM schema_migrations"))
    return {row[0] for row in result.fetchall()}


async def _is_established_db(db) -> bool:
    """Post-017 marker: dag_nodes.is_output_node column exists.

    More specific than 'jobs' table existence (which is true after migration
    001) — ensures we only seed the baseline on DBs that actually received
    migrations 002–017 out-of-band.
    """
    result = await db.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'dag_nodes' "
        "  AND column_name = 'is_output_node' "
        "  AND table_schema = current_schema()"
    ))
    return result.first() is not None


async def _seed_baseline_if_established(db, applied: set[str]) -> None:
    """Seed pre-runner baseline as already-applied on established DBs."""
    if applied:
        return
    if not await _is_established_db(db):
        return
    logger.warning(
        "migrations_seed_baseline: post-017 marker present with no "
        "schema_migrations entries; seeding %d baseline files as applied",
        len(_PRE_RUNNER_BASELINE),
    )
    for fname in sorted(_PRE_RUNNER_BASELINE):
        await db.execute(
            text("INSERT INTO schema_migrations (filename) VALUES (:f) "
                 "ON CONFLICT (filename) DO NOTHING"),
            {"f": fname},
        )


def _has_own_transaction(sql: str) -> bool:
    """True if the migration file authors its own outer BEGIN/COMMIT.

    asyncpg refuses BEGIN/COMMIT inside an active transaction. The runner
    always wraps applies in an outer SQLAlchemy transaction (with
    per-migration SAVEPOINTs), so any author-supplied outer BEGIN/COMMIT
    must be stripped before the body is executed.
    """
    import re
    return bool(re.search(r"^\s*BEGIN\s*;", sql, re.IGNORECASE | re.MULTILINE))


def _strip_outer_transaction(sql: str) -> str:
    """Strip the leading BEGIN; and trailing COMMIT; from a migration.

    Atomicity is preserved by the per-migration SAVEPOINT opened by
    ``_apply_one`` via ``db.begin_nested()``. Only the outermost pair is
    stripped; nested BEGIN/COMMIT (none currently present) would still
    fail — and should, because asyncpg can't run them mid-transaction.
    """
    import re
    sql = re.sub(r"^\s*BEGIN\s*;", "", sql, count=1, flags=re.IGNORECASE | re.MULTILINE)
    sql = re.sub(r"\s*COMMIT\s*;\s*\Z", "", sql, count=1, flags=re.IGNORECASE)
    return sql


async def _apply_one(db, path: Path) -> None:
    """Apply a single migration + record it inside a SAVEPOINT.

    The runner holds the outer SQLAlchemy transaction (and the advisory
    lock) for the whole apply run. Each migration runs in a SAVEPOINT
    (``db.begin_nested()``); on failure, only that migration's effects
    roll back, the outer txn keeps successfully-applied migrations alive,
    and the runner returns an error after which the outer commit
    persists everything that did succeed.
    """
    sql = path.read_text()
    if not sql.strip():
        logger.warning("migration_empty_skipped: file=%s", path.name)
        return

    if _has_own_transaction(sql):
        sql = _strip_outer_transaction(sql)

    async with db.begin_nested():
        conn = await db.connection()
        await conn.exec_driver_sql(sql)
        await db.execute(
            text("INSERT INTO schema_migrations (filename) VALUES (:f)"),
            {"f": path.name},
        )
    logger.info("migration_applied: file=%s", path.name)


async def run_migrations() -> dict:
    """Apply any unapplied migrations. Returns a summary dict.

    Holds a single outer transaction across the entire run so the
    transactional advisory lock is retained for the full apply loop —
    concurrent runners block until the holder commits or rolls back.
    Each migration runs in a SAVEPOINT so a single failure rolls back
    only that migration; the outer commit then persists any earlier
    successes.
    """
    if not _MIGRATIONS_DIR.exists():
        logger.warning("migrations_dir_missing: path=%s", _MIGRATIONS_DIR)
        return {"status": "skipped", "reason": "dir_missing", "applied": []}

    files = sorted(_MIGRATIONS_DIR.glob("*.sql"))
    if not files:
        return {"status": "ok", "applied": []}

    applied_this_run: list[str] = []

    async with async_session() as db:
        async with db.begin():
            await db.execute(
                text("SELECT pg_advisory_xact_lock(:k)"),
                {"k": _ADVISORY_LOCK_KEY},
            )
            await _ensure_tracking_table(db)
            already_applied = await _get_applied(db)
            await _seed_baseline_if_established(db, already_applied)
            already_applied = await _get_applied(db)

            pending = [p for p in files if p.name not in already_applied]

            for path in pending:
                try:
                    await _apply_one(db, path)
                    applied_this_run.append(path.name)
                except Exception as exc:
                    logger.error(
                        "migration_failed: file=%s error=%s",
                        path.name, exc,
                    )
                    return {
                        "status": "error",
                        "error": str(exc),
                        "failed_file": path.name,
                        "applied": applied_this_run,
                    }

    logger.info(
        "migrations_complete: applied_count=%d total_files=%d",
        len(applied_this_run), len(files),
    )
    return {
        "status": "ok",
        "applied": applied_this_run,
        "total": len(files),
    }


if __name__ == "__main__":
    result = asyncio.run(run_migrations())
    print(result)
    if result.get("status") == "error":
        sys.exit(1)
