-- 018: Expand scheduled_jobs.last_status CHECK to include 'timeout'.
-- Idempotent: drops constraint if present, then re-adds expanded version.
-- Preserves original values ('success','failed','running',NULL) and adds 'timeout'.

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE constraint_name = 'scheduled_jobs_last_status_check'
          AND table_name = 'scheduled_jobs'
    ) THEN
        ALTER TABLE scheduled_jobs
            DROP CONSTRAINT scheduled_jobs_last_status_check;
    END IF;

    ALTER TABLE scheduled_jobs
        ADD CONSTRAINT scheduled_jobs_last_status_check
        CHECK (last_status IS NULL OR last_status IN ('success', 'failed', 'running', 'timeout'));
END $$;
