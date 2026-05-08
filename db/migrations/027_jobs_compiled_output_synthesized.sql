-- 027_jobs_compiled_output_synthesized.sql
-- Sprint X.2 — surface the W.7 LLM-synthesis decision on /exec/status.
--
-- Tracks whether `compiled_output` is the LLM-synthesized narrative
-- (settings.compile_synthesis_enabled=true + synthesis succeeded) or the
-- raw heuristic body (synthesis disabled, fail-open, or CodeGen-guarded).
-- Read by app/modules/execution_handler.execution_status; written by
-- app/modules/execution_agent.execute_next_node when it persists
-- compiled_output. Default FALSE so existing rows (pre-W.7) carry the
-- semantically correct legacy state.
--
-- Idempotent (IF NOT EXISTS) — re-applying is a no-op.

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS compiled_output_synthesized BOOLEAN NOT NULL DEFAULT FALSE;
