-- Scaffold Engine — PostgreSQL Schema
-- Creates the 8 core tables that existed at project inception (#87).
-- Additional tables (dedup_log, research_sessions, scheduled_jobs,
-- apscheduler_jobs, prompt_revisions, assist_sessions, assist_steps,
-- model_costs, llm_call_logs, system_alerts, + legacy) come from
-- migrations 002–033.
-- Idempotent (safe to re-run).
--
-- Baseline currency (§17.94 refresh — post-migration-033 state):
--   * jobs.status: 14 statuses incl. assisted_*
--   * jobs.compiled_output_synthesized: BOOLEAN (mig 027)
--   * jobs.compile_synthesis_override: BOOLEAN NULL (mig 029)
--   * dag_nodes.last_verification_reason: TEXT NULL (mig 026)
--   * error_logs.error_type: 4 values ('model_failure', 'structural' dropped)
-- ALTER-style migrations that touched the core tables have been folded
-- in below; new tables created by migrations stay in their own files
-- (the migration runner applies them on every startup, and the listed
-- comments above point future readers at the right place to look).
-- The migration runner advances any DB that bootstraps from a stricter
-- baseline by reapplying 002-033 in order.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. jobs: Top-level workflow tracking
CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'refining', 'awaiting_confirmation', 'researching',
            'planning', 'executing', 'running',
            'completed', 'failed', 'cancelled', 'blocked',
            'assisted_executing', 'assisted_running', 'assisted_paused'
        )),
    input_text TEXT,
    refined_brief JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    error_summary TEXT,
    compiled_output TEXT,
    -- mig 007 (Sprint E ideation workflow): structured research blob
    -- + plain-text workflow summary written by Phase 2 (research → ingest
    -- → compile). research_data is JSONB so downstream consumers can
    -- query specific keys; workflow_summary is human-readable.
    research_data JSONB,
    workflow_summary TEXT,
    compiled_output_synthesized BOOLEAN NOT NULL DEFAULT FALSE,
    -- mig 029 (X.6): NULL = inherit settings.compile_synthesis_enabled;
    -- TRUE/FALSE force per-job override. No DEFAULT so existing rows
    -- and new rows start NULL (inherit).
    compile_synthesis_override BOOLEAN,
    metadata JSONB DEFAULT '{}'::jsonb
);

-- 2. dag_nodes: Individual DAG node state
CREATE TABLE IF NOT EXISTS dag_nodes (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    node_key TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    node_type TEXT NOT NULL DEFAULT 'task'
        CHECK (node_type IN ('task', 'decision', 'parallel_group', 'checkpoint')),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'done', 'failed', 'skipped')),
    depends_on TEXT[] DEFAULT '{}',
    assigned_model TEXT,
    prompt_template TEXT,
    optimized_prompt TEXT,
    output_text TEXT,
    output_artifact_id UUID,
    confidence FLOAT DEFAULT NULL,
    -- domain values are constrained to app.config.VALID_DOMAINS
    -- ('prompt', 'rag', 'eng', 'llm', 'spec' — longest=6); width=10 leaves
    -- headroom but is intentionally narrow so any drift is caught at INSERT.
    domain VARCHAR(10) DEFAULT NULL,
    -- tool values are constrained to app.config.VALID_TOOLS
    -- ('LLM', 'CodeGen', 'SearXNG', 'Milvus' — longest=7); width=50 is
    -- legacy-wide and could be tightened in a future migration if drift
    -- becomes an issue.
    tool VARCHAR(50) DEFAULT 'LLM',
    retry_count INT NOT NULL DEFAULT 0,
    max_retries INT NOT NULL DEFAULT 3,
    parallel_group INT,
    execution_order INT,
    -- mig 017 (#97): explicit leaf marker set by the DAG generator at
    -- INSERT time. _compile_output prefers explicit markers (Strategy 0)
    -- before falling back to title-heuristic / last-CodeGen / concat.
    is_output_node BOOLEAN NOT NULL DEFAULT FALSE,
    -- mig 026 (W.1): the most recent verifier-rejection reason. Read by
    -- execution_agent._build_prompt on retry to prepend a "Reviewer
    -- feedback" block to the next attempt's user message. Intentionally
    -- NOT nulled on retry-reset — the whole point of the feedback loop
    -- is that the next attempt sees what the previous one got wrong.
    last_verification_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    UNIQUE(job_id, node_key)
);

-- 3. execution_logs: Structured JSON logs per node
CREATE TABLE IF NOT EXISTS execution_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    node_id UUID REFERENCES dag_nodes(id) ON DELETE SET NULL,
    log_level TEXT NOT NULL DEFAULT 'info'
        CHECK (log_level IN ('debug', 'info', 'warning', 'error', 'critical')),
    message TEXT NOT NULL,
    details JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 4. error_logs: Error details with recovery tracking
CREATE TABLE IF NOT EXISTS error_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID REFERENCES jobs(id) ON DELETE CASCADE,
    node_id UUID REFERENCES dag_nodes(id) ON DELETE SET NULL,
    error_type TEXT NOT NULL
        CHECK (error_type IN ('transient', 'timeout', 'validation', 'unrecoverable')),
    error_message TEXT NOT NULL,
    stack_trace TEXT,
    model_used TEXT,
    retry_count INT NOT NULL DEFAULT 0,
    recovery_action TEXT
        CHECK (recovery_action IS NULL OR recovery_action IN ('retry', 'model_swap', 'dag_replan', 'manual', 'none')),
    recovery_model TEXT,
    resolved BOOLEAN NOT NULL DEFAULT FALSE,
    resolution TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

-- 5. artifacts: References to generated outputs
CREATE TABLE IF NOT EXISTS artifacts (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    node_id UUID REFERENCES dag_nodes(id) ON DELETE SET NULL,
    artifact_type TEXT NOT NULL
        CHECK (artifact_type IN ('dag', 'prompt', 'toon_file', 'plan', 'code', 'report', 'mermaid', 'other')),
    title TEXT NOT NULL,
    content TEXT,
    file_path TEXT,
    mime_type TEXT DEFAULT 'text/plain',
    size_bytes INT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 6. (removed) performance_logs — replaced by llm_call_logs (migration 030).
--    Dead writer log_model_call() was never called after J.3.a; table dropped
--    by migration 031.

-- 7. benchmark_results: Model accuracy benchmarks per domain
CREATE TABLE IF NOT EXISTS benchmark_results (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    model TEXT NOT NULL,
    domain TEXT NOT NULL,
    benchmark_name TEXT NOT NULL,
    score FLOAT NOT NULL,
    max_score FLOAT DEFAULT 1.0,
    sample_count INT,
    details JSONB DEFAULT '{}'::jsonb,
    run_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- 8. blockers: Beta blocker tracking
CREATE TABLE IF NOT EXISTS blockers (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title TEXT NOT NULL,
    description TEXT,
    severity TEXT NOT NULL DEFAULT 'medium'
        CHECK (severity IN ('critical', 'high', 'medium', 'low')),
    category TEXT
        CHECK (category IS NULL OR category IN ('infrastructure', 'model', 'pipeline', 'ui', 'data', 'performance', 'other')),
    status TEXT NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'in_progress', 'resolved', 'wont_fix')),
    resolution TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    resolved_at TIMESTAMPTZ
);

-- Indexes for common query patterns
CREATE INDEX IF NOT EXISTS idx_dag_nodes_job_id ON dag_nodes(job_id);
CREATE INDEX IF NOT EXISTS idx_dag_nodes_status ON dag_nodes(status);
CREATE INDEX IF NOT EXISTS idx_execution_logs_job_id ON execution_logs(job_id);
CREATE INDEX IF NOT EXISTS idx_execution_logs_created ON execution_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_error_logs_job_id ON error_logs(job_id);
CREATE INDEX IF NOT EXISTS idx_error_logs_resolved ON error_logs(resolved);
CREATE INDEX IF NOT EXISTS idx_artifacts_job_id ON artifacts(job_id);
CREATE INDEX IF NOT EXISTS idx_blockers_status ON blockers(status);
-- idx_jobs_status was added with the original baseline (pre-runner). No
-- migration file declares it because it predates the schema_migrations
-- table; documented here so future audits don't re-flag it as orphaned.
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
-- This index is also (re-)created by migration 006_add_indexes.sql
-- (CREATE INDEX IF NOT EXISTS makes both sides idempotent). Kept here so a
-- DB bootstrapping straight from init.sql has it without waiting on the
-- runner; migration 006 is the historical source of record.
-- (idx_performance_logs_job_id was here pre-031; removed when the
-- performance_logs table was dropped.)
CREATE INDEX IF NOT EXISTS idx_dag_nodes_domain ON dag_nodes(domain);
-- mig 017: partial index for the explicit-marker path in _compile_output.
CREATE INDEX IF NOT EXISTS idx_dag_nodes_output_node
    ON dag_nodes (job_id, is_output_node)
    WHERE is_output_node = TRUE;

-- Updated_at trigger function
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Auto-update updated_at on relevant tables
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_jobs_updated_at') THEN
        CREATE TRIGGER trg_jobs_updated_at BEFORE UPDATE ON jobs
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_dag_nodes_updated_at') THEN
        CREATE TRIGGER trg_dag_nodes_updated_at BEFORE UPDATE ON dag_nodes
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_blockers_updated_at') THEN
        CREATE TRIGGER trg_blockers_updated_at BEFORE UPDATE ON blockers
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;

-- Migration tracking table (also created by app/migrations.py on first run).
-- Declared here so fresh DB init has it available before the runner executes.
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
