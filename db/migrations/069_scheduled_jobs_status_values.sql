-- 069_scheduled_jobs_status_values.sql
-- §17.812 (audit C8) — widen scheduled_jobs.last_status to record HONEST terminal
-- states. The scheduler now reconciles a scheduled research run's last_status
-- against the research SESSION's actual outcome — a swallowed research failure
-- (finalized 'failed' inside run_research, yielded as an SSE error, then returned
-- normally) previously left last_status='success', so /schedule list lied — and
-- it records 'skipped' when the singleton-running guard refused to start a
-- session (a colliding schedule that silently no-op'd every run). Both
-- 'cancelled' and 'skipped' were outside the old CHECK set.
--
-- Single statement (one ALTER TABLE with comma-separated actions) per the
-- asyncpg prepared-statement migration invariant (§17.140).
ALTER TABLE scheduled_jobs
    DROP CONSTRAINT IF EXISTS scheduled_jobs_last_status_check,
    ADD CONSTRAINT scheduled_jobs_last_status_check
        CHECK (last_status IS NULL OR last_status IN
               ('success', 'failed', 'running', 'timeout', 'cancelled', 'skipped'));
