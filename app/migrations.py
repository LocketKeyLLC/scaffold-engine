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

# Files folded into the db/init.sql baseline (§17.94 — "post-migration-033
# state"). On a FRESH DB these objects are created by init.sql, so the runner
# must NOT re-execute the migration files (several are multi-statement and the
# asyncpg path would reject them; the core-table ALTERs would also revert folded
# state) — they are seeded into schema_migrations as already-applied instead
# (see _seed_baseline_if_established). On an established DB schema_migrations is
# already populated, so seeding is a no-op. Migrations > 033 are applied normally
# by the runner.
#
# §17.535 — extended 002–017 → 002–033 to match the actual init.sql currency.
# The old 002–017 range left the table-creating migrations 018–033 to be run by
# the runner on a fresh DB, but it halted at the multi-statement 020 (and earlier
# wrongly seeded 009/010/011 as applied without their tables existing in init.sql)
# — so a fresh bootstrap produced an incomplete schema. init.sql now declares all
# 002–033 objects, so the whole range is correctly seeded here.
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
    "018_scheduled_jobs_last_status_check.sql",
    "019_dag_nodes_unique_job_node_key.sql",
    "020_research_sessions_single_running.sql",
    "021_updated_at_triggers.sql",
    "022_prompt_revisions.sql",
    "023_assist_mode.sql",
    "024_drop_assist_steps_applied_status.sql",
    "025_drop_dead_error_types.sql",
    "026_dag_nodes_last_verification_reason.sql",
    "027_jobs_compiled_output_synthesized.sql",
    "028_research_sessions_last_activity_at.sql",
    "029_jobs_compile_synthesis_override.sql",
    "030_cost_telemetry.sql",
    "031_drop_performance_logs.sql",
    "032_system_alerts.sql",
    "033_llm_call_logs_call_kind.sql",
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
    """Post-033 marker: llm_call_logs.call_kind column exists (added by mig 033).

    The seed range is now the full init.sql baseline (002–033, §17.535), so the
    marker must prove the DB carries that whole baseline — not merely the old
    017 marker (dag_nodes.is_output_node), which a stale through-017 DB could
    have without the 018–033 objects. call_kind is the last column the through-033
    baseline adds and is declared in init.sql, so its presence ⟺ a complete
    through-033 bootstrap (or a fully-migrated DB, where schema_migrations is
    already non-empty and seeding is skipped before this is consulted).
    """
    result = await db.execute(text(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = 'llm_call_logs' "
        "  AND column_name = 'call_kind' "
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
        "migrations_seed_baseline: post-033 marker present with no "
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
    # path.read_text is sync I/O; offload so a slow disk doesn't block the
    # event loop while the advisory lock is held.
    loop = asyncio.get_running_loop()
    sql = await loop.run_in_executor(None, path.read_text)
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
