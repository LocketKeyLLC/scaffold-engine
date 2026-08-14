-- 061_jobs_crash_resume_counters.sql
-- §17.774 — automatic crash-resume of orphaned mid-execution jobs.
--
-- After a process crash (SIGKILL / OOM / power loss) the in-flight node's
-- 'running' status is reset to 'pending' by the lifespan sweep, but the parent
-- job stays 'running' and nothing re-launches it. §17.774 adds a startup
-- resume pass (app/modules/execution_resume.py) that re-drives execute_all_nodes
-- for such jobs, picking up at the reset node and reusing every already-'done'
-- node's output.
--
-- These two counters implement the crash-loop guard so a node that reliably
-- kills the process (e.g. an OOMing LLM call) is marked 'failed' instead of
-- restart-storming on every boot:
--   resume_attempts    — consecutive resume launches that made NO new progress.
--                        Incremented when a restart adds zero new 'done' nodes;
--                        reset to 1 when progress was made since the last resume.
--                        Exceeding settings.execution_max_resume_attempts fails
--                        the job with error_summary 'crash_resume_budget_exhausted'.
--   resume_done_marker — count of 'done' nodes observed at the last resume launch;
--                        the progress yardstick the counter compares against.
--
-- ONE statement (single DO block) per the asyncpg "no multiple commands in a
-- prepared statement" rule (§17.140). All additive / IF NOT EXISTS — safe to
-- re-run; existing rows backfill to 0 via the column default.

DO $mig$
BEGIN
    ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS resume_attempts    INTEGER NOT NULL DEFAULT 0,
        ADD COLUMN IF NOT EXISTS resume_done_marker INTEGER NOT NULL DEFAULT 0;

    COMMENT ON COLUMN jobs.resume_attempts IS
        'Consecutive zero-progress crash-resume launches (§17.774). Cap = settings.execution_max_resume_attempts.';
    COMMENT ON COLUMN jobs.resume_done_marker IS
        'Count of done dag_nodes at the last crash-resume launch (§17.774 progress yardstick).';
END
$mig$;
