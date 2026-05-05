-- Migration 011: Scheduled research jobs
-- Adds user-facing schedule metadata + APScheduler's internal jobstore table
-- Date: April 16, 2026

BEGIN;

-- User-facing schedule definitions (what Adam sees/manages)
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id              SERIAL PRIMARY KEY,
    topic           TEXT NOT NULL,
    depth           TEXT NOT NULL DEFAULT 'medium' CHECK (depth IN ('shallow', 'medium', 'deep')),
    cron_expression TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at     TIMESTAMPTZ,
    last_status     TEXT CHECK (last_status IN ('success', 'failed', 'running', NULL)),
    last_job_id     TEXT,
    next_run_at     TIMESTAMPTZ,
    run_count       INTEGER NOT NULL DEFAULT 0,
    failure_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_enabled ON scheduled_jobs (enabled) WHERE enabled = TRUE;
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_next_run ON scheduled_jobs (next_run_at) WHERE enabled = TRUE;

-- APScheduler's internal jobstore (managed by SQLAlchemyJobStore, schema is APScheduler's)
-- We pre-create the table so migrations stay in our control rather than runtime DDL
--
-- ╔══════════════════════════════════════════════════════════════════════════╗
-- ║ DO NOT alter `next_run_time` away from DOUBLE PRECISION (epoch seconds). ║
-- ║                                                                          ║
-- ║ APScheduler's SQLAlchemyJobStore reads/writes this column as a numeric   ║
-- ║ POSIX timestamp. Changing the type to TIMESTAMPTZ (or anything else)     ║
-- ║ silently breaks the scheduler's index queries and missed-fire detection. ║
-- ║                                                                          ║
-- ║ The user-facing `scheduled_jobs.next_run_at` is TIMESTAMPTZ; the two are ║
-- ║ deliberately different types and live in separate tables. Do NOT JOIN    ║
-- ║ them without explicit `to_timestamp(next_run_time)` conversion.          ║
-- ║                                                                          ║
-- ║ Coordinate any apscheduler_jobs schema change with the pinned APScheduler║
-- ║ version in requirements.txt. See docs/audit/drift-findings.md.           ║
-- ╚══════════════════════════════════════════════════════════════════════════╝
CREATE TABLE IF NOT EXISTS apscheduler_jobs (
    id              VARCHAR(191) PRIMARY KEY,
    next_run_time   DOUBLE PRECISION,
    job_state       BYTEA NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_apscheduler_jobs_next_run_time ON apscheduler_jobs (next_run_time);

COMMIT;
