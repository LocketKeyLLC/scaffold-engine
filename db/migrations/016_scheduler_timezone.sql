-- Migration 016: Per-schedule timezone for cron triggers.
-- Fixes scheduler fix #8 (cron timezone hardcoded UTC).
-- scheduled_jobs.last_job_id remains TEXT; it will be populated with
-- research_sessions.id (UUID string) by _execute_research_job going forward.

BEGIN;

ALTER TABLE scheduled_jobs
    ADD COLUMN IF NOT EXISTS timezone VARCHAR(64) NOT NULL DEFAULT 'UTC';

COMMENT ON COLUMN scheduled_jobs.timezone IS
    'IANA timezone name (e.g. "America/New_York") for interpreting cron_expression. Defaults to UTC.';

COMMIT;
