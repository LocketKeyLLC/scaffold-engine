-- §17.817 (plan 5.7) — small server-side key/value flags. First consumer:
-- first_run_completed (the connect-models wizard's completion marker — the
-- audit flagged localStorage-only first-run state as a LOW: it re-triggers
-- per browser). Single statement (asyncpg prepared-statement runner).
CREATE TABLE IF NOT EXISTS system_flags (
    key        TEXT PRIMARY KEY,
    value      JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
