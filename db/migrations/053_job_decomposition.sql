-- 053_job_decomposition.sql
-- §17.525 — triage-time task decomposition: an umbrella job groups N component
-- child jobs, each of which runs its own DAG. A single DO-block is one statement
-- on the migration runner's asyncpg path (cf. 043_jobs_job_type.sql); idempotent
-- via ADD COLUMN IF NOT EXISTS + drop-then-add for the FK/CHECK constraints.
DO $$
BEGIN
    ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS parent_job_id UUID,
        ADD COLUMN IF NOT EXISTS component_index INT;

    -- Self-FK. ON DELETE SET NULL (NOT CASCADE): deleting an umbrella must
    -- never destroy live/finished child work — children are orphaned gracefully
    -- and keep their results.
    ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_parent_job_id_fkey;
    ALTER TABLE jobs
        ADD CONSTRAINT jobs_parent_job_id_fkey
        FOREIGN KEY (parent_job_id) REFERENCES jobs(id) ON DELETE SET NULL;

    -- Widen job_type: 'umbrella' (the thin grouping parent — no DAG, never
    -- executes) and 'component' (a child job).
    ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_job_type_check;
    ALTER TABLE jobs
        ADD CONSTRAINT jobs_job_type_check
        CHECK (job_type IN ('legacy', 'design_circuit', 'umbrella', 'component'));

    -- Widen status: 'aggregating' = umbrella alive, children still running.
    -- Deliberately absent from every cleanup reaper whitelist, so umbrellas are
    -- inert to the normal stale sweep (a dedicated umbrella-finalize sweep
    -- handles them — see cleanup.py).
    ALTER TABLE jobs DROP CONSTRAINT IF EXISTS jobs_status_check;
    ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
        CHECK (status IN (
            'pending', 'refining', 'awaiting_confirmation', 'researching',
            'planning', 'executing', 'running',
            'completed', 'failed', 'cancelled', 'blocked',
            'assisted_executing', 'assisted_running', 'assisted_paused',
            'aggregating'
        ));

    CREATE INDEX IF NOT EXISTS idx_jobs_parent_job_id
        ON jobs(parent_job_id) WHERE parent_job_id IS NOT NULL;
END $$;
