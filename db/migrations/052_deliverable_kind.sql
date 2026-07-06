-- Migration 052: machine-readable deliverable kind (§17.519)
--
-- The §17.506 / §17.516 banners tell a HUMAN whether a completed job's
-- deliverable was actually executed, is an unexecuted plan/runbook, or was
-- carried out by the operator via Assist Mode. But the only signal was banner
-- TEXT inside compiled_output — programmatic consumers (web UI, SDK, /jobs
-- filters, dashboards) could not branch on it without string-matching.
--
-- This adds a queryable column set at compile/finalize time:
--   deliverable_kind:
--     'executed'         — autonomous run produced real output (code, docs, …)
--     'plan_only'        — autonomous run produced unexecuted Shell runbooks
--                          (shell_tool_enabled=False); nothing was performed
--     'assist_completed' — operator executed + verified the steps via Assist
--     NULL               — no deliverable (e.g. every node skipped) / pre-052 job
--
-- ONE statement (asyncpg prepared-statement path rejects multi-command files).
-- Idempotent via ADD COLUMN IF NOT EXISTS.

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS deliverable_kind TEXT
        CHECK (deliverable_kind IN ('executed', 'plan_only', 'assist_completed'));
