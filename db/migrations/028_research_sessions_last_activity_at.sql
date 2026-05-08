-- 028_research_sessions_last_activity_at.sql
-- Why: separate "last meaningful activity" from "last DB write" so the
--      cleanup reaper can distinguish a genuinely-idle session from one
--      that was merely renamed / metadata-touched. Mirrors the
--      assist_sessions.last_activity_at pattern from migration 023.
-- Idempotent: single DO block + IF NOT EXISTS guards. Re-runs are no-ops.
--
-- Single-statement form (DO $$ ... END $$;) chosen because the migration
-- runner uses asyncpg, whose prepared-statement protocol rejects
-- multi-statement bodies. Wrapping ALTER + UPDATE + CREATE INDEX in one
-- DO block keeps the file as a single top-level statement.

DO $$
BEGIN
    -- 1. Add column if missing. The backfill runs ONLY on the first
    --    apply (gated by the IF NOT EXISTS column check) so re-runs
    --    don't clobber values written by application code.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name   = 'research_sessions'
           AND column_name  = 'last_activity_at'
    ) THEN
        ALTER TABLE research_sessions
            ADD COLUMN last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

        UPDATE research_sessions
           SET last_activity_at = COALESCE(updated_at, created_at);
    END IF;

    -- 2. Partial index for the cleanup reaper. Mirrors the existing
    --    idx_research_sessions_active_updated (kept; the listing endpoint
    --    still ORDERs by updated_at DESC).
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = current_schema()
           AND indexname  = 'idx_research_sessions_active_activity'
    ) THEN
        EXECUTE 'CREATE INDEX idx_research_sessions_active_activity
            ON research_sessions(status, last_activity_at DESC)
            WHERE status IN (''pending'', ''running'')';
    END IF;
END $$;
