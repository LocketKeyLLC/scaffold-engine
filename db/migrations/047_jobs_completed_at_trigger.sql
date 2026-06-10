-- 047_jobs_completed_at_trigger.sql
-- §17.466 — stamp jobs.completed_at automatically whenever a job enters a
-- terminal state (completed / failed / cancelled), and clear it on any
-- transition back to a non-terminal state.
--
-- Root cause it fixes: the mainline DAG-job completion path (the two
-- execution_agent.py autocomplete UPDATEs) and the reaper's terminal UPDATEs
-- (cleanup.py running->failed / long-phase->failed / planning->cancelled /
-- awaiting->cancelled) set jobs.status but never jobs.completed_at. Only Assist
-- Mode (assist_agent.py) ever stamped it. Result observed live: 0 of 176
-- terminal jobs had completed_at populated — the column was effectively dead
-- and any duration / finished-at consumer would read NULL.
--
-- Fix shape: a trigger (mirrors the existing update_updated_at() trigger in
-- init.sql) so EVERY current and future writer is covered in one place rather
-- than editing ~13 scattered SQL sites and hoping none is missed. Invariant
-- enforced: completed_at IS NOT NULL  <=>  status is terminal.
--   - IS-NULL guard keeps it idempotent: the post-completion compiled_output
--     UPDATE (execution_agent) re-fires the trigger but does NOT re-stamp.
--   - ELSE branch clears a stale stamp when a terminal job is re-opened
--     (e.g. retry_failed_node: blocked/failed -> executing), so a re-run's
--     completion time is the one that survives.
--
-- The whole migration is ONE statement (a single DO block) per the asyncpg
-- "no multiple commands in a prepared statement" rule (§17.140); the inner DDL
-- is run via EXECUTE with nested dollar-quote tags ($mig$ / $fn$ / $body$).

DO $mig$
BEGIN
    -- 1. Historical backfill — runs BEFORE the trigger is attached. updated_at
    --    is the best surviving proxy for when these jobs finished (the terminal
    --    UPDATE touched it last). trg_jobs_updated_at is disabled around it so
    --    writing completed_at does not bump updated_at on the finished rows
    --    (which would cluster them all at "now" in the updated_at-DESC list).
    EXECUTE 'ALTER TABLE jobs DISABLE TRIGGER trg_jobs_updated_at';

    UPDATE jobs
    SET completed_at = updated_at
    WHERE completed_at IS NULL
      AND status IN ('completed', 'failed', 'cancelled');

    EXECUTE 'ALTER TABLE jobs ENABLE TRIGGER trg_jobs_updated_at';

    -- 2. Go-forward trigger function.
    EXECUTE $fn$
        CREATE OR REPLACE FUNCTION stamp_job_completed_at()
        RETURNS TRIGGER AS $body$
        BEGIN
            IF NEW.status IN ('completed', 'failed', 'cancelled') THEN
                IF NEW.completed_at IS NULL THEN
                    NEW.completed_at = NOW();
                END IF;
            ELSE
                NEW.completed_at = NULL;
            END IF;
            RETURN NEW;
        END;
        $body$ LANGUAGE plpgsql;
    $fn$;

    -- 3. Attach the trigger if absent (idempotent, matching the init.sql
    --    updated_at-trigger pattern). BEFORE INSERT OR UPDATE so a job inserted
    --    directly in a terminal state is stamped too, not only transitions.
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_jobs_completed_at'
    ) THEN
        EXECUTE 'CREATE TRIGGER trg_jobs_completed_at '
                'BEFORE INSERT OR UPDATE ON jobs '
                'FOR EACH ROW EXECUTE FUNCTION stamp_job_completed_at()';
    END IF;
END
$mig$;
