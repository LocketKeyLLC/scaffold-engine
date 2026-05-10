-- 033_llm_call_logs_call_kind.sql
-- §17.90 W.7 follow-up — synthesis budget telemetry.
--
-- Adds a `call_kind` column to `llm_call_logs` so the cost rollup can
-- split per-call categories (currently "synthesis" only — see
-- app/modules/execution_compile._synthesize_compiled_output). Other
-- LLM call sites leave the column NULL, which the rollup folds into
-- a generic "uncategorized" bucket.
--
-- Idempotent — IF NOT EXISTS guard on both the column and the index.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name   = 'llm_call_logs'
           AND column_name  = 'call_kind'
    ) THEN
        EXECUTE 'ALTER TABLE llm_call_logs
                 ADD COLUMN call_kind TEXT';
    END IF;

    -- Partial index — only rows with a non-NULL kind are queryable by
    -- category. NULL rows are the common case (every non-synthesis call
    -- today), so a full index would waste space.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = current_schema()
           AND indexname  = 'idx_llm_call_logs_call_kind'
    ) THEN
        EXECUTE 'CREATE INDEX idx_llm_call_logs_call_kind
                 ON llm_call_logs(call_kind)
                 WHERE call_kind IS NOT NULL';
    END IF;
END $$;
