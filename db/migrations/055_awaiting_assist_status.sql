-- §17.624 — new job status 'awaiting_assist'.
--
-- The autonomous executor now PARKS a job whose DAG is predominantly hands-on
-- (Shell steps with no shell backend, or human steps) in 'awaiting_assist'
-- instead of fabricating runbook "done" output and rolling up to a misleading
-- 'completed'. The job's nodes stay 'pending' and the operator drives real
-- execution via /assist. This status is terminal for umbrella roll-up and is a
-- valid /assist start status.
--
-- Single-statement migration: one ALTER with two comma-separated actions
-- (drop + re-add the CHECK), per the runner's single-statement requirement.
ALTER TABLE jobs
    DROP CONSTRAINT jobs_status_check,
    ADD CONSTRAINT jobs_status_check CHECK (status = ANY (ARRAY[
        'pending', 'refining', 'awaiting_confirmation', 'researching',
        'planning', 'executing', 'running', 'completed', 'failed',
        'cancelled', 'blocked', 'assisted_executing', 'assisted_running',
        'assisted_paused', 'aggregating', 'awaiting_assist'
    ]));
