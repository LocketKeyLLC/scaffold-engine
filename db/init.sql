-- Scaffold Engine — PostgreSQL Schema
-- Creates the 8 core tables that existed at project inception (#87).
-- Additional tables (dedup_log, research_sessions, scheduled_jobs,
-- apscheduler_jobs, prompt_revisions, assist_sessions, assist_steps,
-- + legacy) come from migrations 002–025.
-- Idempotent (safe to re-run).
--
-- Baseline currency: this file expresses post-migration-025 state for
-- jobs.status (14 statuses incl. assisted_*) and error_logs.error_type
-- (4 values, 'model_failure' and 'structural' dropped). The migration
-- runner advances any DB that bootstraps from a stricter baseline by
-- reapplying 002-025 in order.

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
    compiled_output_synthesized BOOLEAN NOT NULL DEFAULT FALSE,
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

-- 6. performance_logs: Latency metrics per model call
CREATE TABLE IF NOT EXISTS performance_logs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID REFERENCES jobs(id) ON DELETE SET NULL,
    node_id UUID REFERENCES dag_nodes(id) ON DELETE SET NULL,
    model TEXT NOT NULL,
    endpoint TEXT NOT NULL,
    request_type TEXT NOT NULL DEFAULT 'generate'
        CHECK (request_type IN ('generate', 'embed', 'rerank', 'classify')),
    ttft_ms INT,
    total_duration_ms INT NOT NULL,
    tokens_prompt INT,
    tokens_completion INT,
    tokens_per_sec FLOAT,
    success BOOLEAN NOT NULL DEFAULT TRUE,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

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
CREATE INDEX IF NOT EXISTS idx_performance_logs_model ON performance_logs(model);
CREATE INDEX IF NOT EXISTS idx_performance_logs_created ON performance_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_blockers_status ON blockers(status);
-- idx_jobs_status was added with the original baseline (pre-runner). No
-- migration file declares it because it predates the schema_migrations
-- table; documented here so future audits don't re-flag it as orphaned.
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
-- The next two indexes are also (re-)created by migration 006_add_indexes.sql
-- (CREATE INDEX IF NOT EXISTS makes both sides idempotent). Kept here so a
-- DB bootstrapping straight from init.sql has them without waiting on the
-- runner; migration 006 is the historical source of record.
CREATE INDEX IF NOT EXISTS idx_dag_nodes_domain ON dag_nodes(domain);
CREATE INDEX IF NOT EXISTS idx_performance_logs_job_id ON performance_logs(job_id);

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
