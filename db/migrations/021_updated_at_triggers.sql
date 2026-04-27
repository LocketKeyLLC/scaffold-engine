-- Migration 021: Auto-update triggers for research_sessions + scheduled_jobs.
-- Reuses the existing update_updated_at() function defined in init.sql.
-- Idempotent via pg_trigger lookup so re-running is a no-op.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_research_sessions_updated_at'
    ) THEN
        CREATE TRIGGER trg_research_sessions_updated_at
            BEFORE UPDATE ON research_sessions
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trg_scheduled_jobs_updated_at'
    ) THEN
        CREATE TRIGGER trg_scheduled_jobs_updated_at
            BEFORE UPDATE ON scheduled_jobs
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;
