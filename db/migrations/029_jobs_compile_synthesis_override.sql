-- 029_jobs_compile_synthesis_override.sql
-- Sprint X.6 — per-job opt-in for the W.7 LLM synthesis pass.
--
-- NULL is the inherits-global state: _maybe_synthesize falls through to
-- settings.compile_synthesis_enabled when the column is NULL. TRUE forces
-- synthesis on for this job regardless of global; FALSE forces it off.
-- No DEFAULT — existing rows + new rows start at NULL (inherit), which
-- is the only safe interpretation: nothing should change behavior until
-- an operator explicitly opts in or out per-job.
--
-- Idempotent (IF NOT EXISTS) — re-applying is a no-op.

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS compile_synthesis_override BOOLEAN;
