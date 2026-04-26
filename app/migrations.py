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
    """True if the migration file already manages its own BEGIN/COMMIT.

    asyncpg refuses BEGIN/COMMIT inside an outer transaction. Files that
    self-manage (e.g., migration 013) must be executed at autocommit.
    """
    import re
    return bool(re.search(r"^\s*BEGIN\s*;", sql, re.IGNORECASE | re.MULTILINE))


async def _apply_one(db, path: Path) -> None:
    """Apply a single migration + record it.

    Uses ``exec_driver_sql`` for the migration body so asyncpg sends raw
    SQL via ``execute()`` instead of a prepared statement — this is the
    only path that accepts multiple semicolon-separated commands.
    The tracking INSERT is parameterized as before.

    Files containing their own ``BEGIN;``/``COMMIT;`` are executed at
    autocommit; the tracking row is then recorded in a follow-up txn.
    All other files run inside a single wrapping transaction so DDL +
    tracking commit together.
    """
    sql = path.read_text()
    if not sql.strip():
        logger.warning("migration_empty_skipped: file=%s", path.name)
        return

    if _has_own_transaction(sql):
        # File controls its own transaction. Acquire a fresh raw asyncpg
        # connection (no SQLAlchemy-managed txn) and run at autocommit,
        # then record the tracking row in a separate transaction.
        async with db.begin():
            conn = await db.connection()
            raw = await conn.get_raw_connection()
            await raw.driver_connection.execute(sql)
            await db.execute(
                text("INSERT INTO schema_migrations (filename) VALUES (:f)"),
                {"f": path.name},
            )
    else:
        async with db.begin():
            conn = await db.connection()
            await conn.exec_driver_sql(sql)
            await db.execute(
                text("INSERT INTO schema_migrations (filename) VALUES (:f)"),
                {"f": path.name},
            )
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
    failed_file: str | None = None

    async with async_session() as db:
        # Advisory lock scoped to this transaction; auto-released on commit
        # or rollback. Serializes concurrent runner invocations.
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
                failed_file = path.name
                logger.error(
                    "migration_failed: file=%s error=%s",
                    path.name, exc,
                )
                return {
                    "status": "error",
                    "error": str(exc),
                    "failed_file": failed_file,
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
