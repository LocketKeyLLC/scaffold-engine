-- 030_cost_telemetry.sql
-- Sprint J.3.a — cost + latency telemetry foundation.
--
-- Two new tables:
--   model_costs    provider/model → input/output USD-per-1M-tokens rates.
--                  Seeded with current cloud-provider rates (operators
--                  bump via SQL or a future /admin endpoint when prices
--                  change). Local Ollama models are intentionally NOT
--                  seeded — `compute_cost_usd` returns 0 when the row
--                  is absent, so any Ollama model is free by default.
--   llm_call_logs  per-LLM-call telemetry. job_id/node_id are nullable
--                  so off-job calls (validate_models, /optimize, etc.)
--                  are still tracked, just ungrouped. cost_usd is
--                  computed at insert time so historical reads don't
--                  drift when model_costs rates are updated.
--
-- Single DO block per X.5's lesson: the migration runner uses asyncpg's
-- prepared-statement protocol, which rejects multi-statement bodies.
-- Wrap CREATE TABLE + seed INSERT + CREATE INDEX in one DO so the file
-- is one top-level statement.
--
-- Idempotent — IF NOT EXISTS guards on every DDL; ON CONFLICT DO NOTHING
-- on the seed so re-runs preserve operator-edited rates.

DO $$
BEGIN
    -- ── 1. model_costs table ──────────────────────────────────────────────
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = current_schema()
           AND table_name   = 'model_costs'
    ) THEN
        EXECUTE 'CREATE TABLE model_costs (
            provider          TEXT NOT NULL,
            model             TEXT NOT NULL,
            input_per_1m_usd  NUMERIC(10, 6) NOT NULL DEFAULT 0,
            output_per_1m_usd NUMERIC(10, 6) NOT NULL DEFAULT 0,
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            PRIMARY KEY (provider, model)
        )';
    END IF;

    -- ── 2. Seed pricing — current cloud-provider rates as of 2026-05.
    --    ON CONFLICT DO NOTHING so operator-edited values are preserved
    --    on re-apply. Local ollama:* models intentionally absent — they
    --    fall through to 0 cost via compute_cost_usd.
    INSERT INTO model_costs (provider, model, input_per_1m_usd, output_per_1m_usd) VALUES
        ('openai',    'gpt-4o',                 2.50, 10.00),
        ('openai',    'gpt-4o-mini',            0.15,  0.60),
        ('openai',    'gpt-4-turbo',           10.00, 30.00),
        ('anthropic', 'claude-opus-4-7',       15.00, 75.00),
        ('anthropic', 'claude-sonnet-4-6',      3.00, 15.00),
        ('anthropic', 'claude-haiku-4-5',       1.00,  5.00)
    ON CONFLICT (provider, model) DO NOTHING;

    -- ── 3. llm_call_logs table ────────────────────────────────────────────
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = current_schema()
           AND table_name   = 'llm_call_logs'
    ) THEN
        EXECUTE 'CREATE TABLE llm_call_logs (
            id                BIGSERIAL PRIMARY KEY,
            job_id            UUID,
            node_id           UUID,
            provider          TEXT NOT NULL,
            model             TEXT NOT NULL,
            prompt_tokens     INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            latency_ms        INTEGER NOT NULL DEFAULT 0,
            cost_usd          NUMERIC(10, 6) NOT NULL DEFAULT 0,
            success           BOOLEAN NOT NULL DEFAULT TRUE,
            created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )';
    END IF;

    -- ── 4. Indexes ────────────────────────────────────────────────────────
    -- Partial on job_id since off-job calls (validate_models, /optimize
    -- standalone) are nullable; full scan on those is fine.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = current_schema()
           AND indexname  = 'idx_llm_call_logs_job_id'
    ) THEN
        EXECUTE 'CREATE INDEX idx_llm_call_logs_job_id
            ON llm_call_logs(job_id)
            WHERE job_id IS NOT NULL';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = current_schema()
           AND indexname  = 'idx_llm_call_logs_created_at'
    ) THEN
        EXECUTE 'CREATE INDEX idx_llm_call_logs_created_at
            ON llm_call_logs(created_at DESC)';
    END IF;
END $$;
