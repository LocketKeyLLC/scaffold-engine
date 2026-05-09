-- Migration 031: Drop the dead performance_logs table.
--
-- The table was added pre-J track to hold per-LLM-call latency metrics,
-- written by `app/middleware/performance.py:log_model_call()`. J.3.a
-- (migration 030) introduced `llm_call_logs` with the same coverage
-- plus cost telemetry, and `_record_call` in model_router replaced the
-- old write path. `log_model_call` was never called after that point,
-- and `performance_logs` has had zero writers since.
--
-- Findings during the X.20 observability rollups sprint surfaced both:
-- the helper is dead code, and the table accumulates nothing. X.20
-- explicitly deferred the cleanup so the rollup work landed cleanly;
-- this migration closes the loop.
--
-- Single DO block per X.5's lesson: the migration runner uses asyncpg's
-- prepared-statement protocol, which rejects multi-statement bodies.
-- Wrap the index drops + table drop in one DO so the file is one
-- top-level statement.
--
-- Idempotent: DROP ... IF EXISTS guards on every statement.

DO $$
BEGIN
    DROP INDEX IF EXISTS idx_performance_logs_model;
    DROP INDEX IF EXISTS idx_performance_logs_created;
    DROP INDEX IF EXISTS idx_performance_logs_job_id;
    DROP TABLE IF EXISTS performance_logs;
END $$;
