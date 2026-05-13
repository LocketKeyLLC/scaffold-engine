-- 043_jobs_job_type.sql
-- §17.151 — Add a discriminator to the jobs table so the new
-- design_circuit pipeline (§17.144 → §17.148 chain) can share the
-- jobs row + status lifecycle with the legacy flows (ideation,
-- research, execution) without colliding on semantic interpretation
-- of status values.
--
-- Existing rows are tagged ``legacy`` so all pre-§17.151 jobs keep
-- their meaning unchanged. New job_type values land here as the
-- pipeline catalogue grows.
--
-- The partial index excludes the dominant ``legacy`` value — a
-- "give me every design_circuit job" lookup is cheap regardless of
-- how big the legacy population gets.
--
-- DO-block wrapped per the asyncpg multi-statement rule (§17.140 /
-- 032_system_alerts.sql).

DO $$
BEGIN
    ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS job_type TEXT NOT NULL DEFAULT 'legacy';

    -- Defensive: re-applying the migration on a DB that already has
    -- the column (from a manual ALTER on a prior session) must not
    -- duplicate the CHECK. Drop-if-exists then add — same as the
    -- §17.94 pattern for evolving constraint sets.
    ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_job_type_check;
    ALTER TABLE jobs
        ADD CONSTRAINT jobs_job_type_check
        CHECK (job_type IN ('legacy', 'design_circuit'));

    CREATE INDEX IF NOT EXISTS idx_jobs_job_type
        ON jobs(job_type)
        WHERE job_type <> 'legacy';
END $$;
