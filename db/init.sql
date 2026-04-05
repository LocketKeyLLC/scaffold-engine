-- Scaffold Engine — PostgreSQL Schema
-- All 8 tables, idempotent (safe to re-run)

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- 1. jobs: Top-level workflow tracking
CREATE TABLE IF NOT EXISTS jobs (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    title TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'refining', 'planning', 'executing', 'completed', 'failed', 'cancelled')),
    input_text TEXT,
    refined_brief JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    error_summary TEXT,
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
    domain VARCHAR(10) DEFAULT NULL,
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
        CHECK (error_type IN ('transient', 'model_failure', 'timeout', 'validation', 'structural', 'unrecoverable')),
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
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);

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
