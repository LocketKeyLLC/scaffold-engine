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

-- ════════════════════════════════════════════════════════════════════════
-- §17.535 — non-core tables created by migrations 009–032 (folded into the
-- through-033 baseline). PREVIOUSLY MISSING from init.sql: the header claimed
-- "post-migration-033 state" and the runner seeds 002–033 as already-applied,
-- but these tables were never declared here — so a FRESH bootstrap created only
-- the 8 core tables, the runner skipped (seeded) the table-creating migrations,
-- and halted at 018 (`scheduled_jobs does not exist`). Declared here at their
-- through-033 shape; later ALTERs (e.g. assist_steps guidance cols, mig 051)
-- are applied by the runner on top (it runs 034+ normally once it no longer
-- halts). Order respects FK deps: assist_sessions before assist_steps.
-- ════════════════════════════════════════════════════════════════════════

-- mig 009: dedup audit log (RAG ingest 3-tier decisions).
CREATE TABLE IF NOT EXISTS dedup_log (
    id                SERIAL PRIMARY KEY,
    new_content_hash  VARCHAR(64) NOT NULL,
    existing_entry_id VARCHAR(255) NOT NULL,
    similarity_score  DOUBLE PRECISION NOT NULL,
    action_taken      VARCHAR(20) NOT NULL DEFAULT 'rejected',
    created_at        TIMESTAMPTZ DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_dedup_log_created_at ON dedup_log(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_dedup_log_existing_entry ON dedup_log(existing_entry_id);

-- mig 010 (+012/013/014/015/020/028): research session lifecycle.
CREATE TABLE IF NOT EXISTS research_sessions (
    id                      UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic                   TEXT NOT NULL,
    depth                   VARCHAR(32) NOT NULL DEFAULT 'medium',
    domain                  VARCHAR(50) NOT NULL DEFAULT 'eng',
    iterations_completed    INTEGER NOT NULL DEFAULT 0,
    total_entries_extracted INTEGER NOT NULL DEFAULT 0,
    total_entries_ingested  INTEGER NOT NULL DEFAULT 0,
    total_entries_rejected  INTEGER NOT NULL DEFAULT 0,
    total_urls_searched     INTEGER NOT NULL DEFAULT 0,
    total_queries           INTEGER NOT NULL DEFAULT 0,
    duration_ms             INTEGER NOT NULL DEFAULT 0,
    coverage_pct            DOUBLE PRECISION,
    status                  VARCHAR(32) NOT NULL DEFAULT 'running',
    summary                 TEXT,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at            TIMESTAMPTZ,
    state_snapshot          JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    error_message           TEXT,
    pause_question          TEXT,
    pause_expires_at        TIMESTAMPTZ,
    pause_reply             TEXT,
    last_activity_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_research_sessions_created_at ON research_sessions(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_research_sessions_status ON research_sessions(status);
CREATE INDEX IF NOT EXISTS idx_research_sessions_active_activity
    ON research_sessions(status, last_activity_at DESC)
    WHERE status IN ('pending', 'running');
CREATE INDEX IF NOT EXISTS idx_research_sessions_active_updated
    ON research_sessions(status, updated_at DESC)
    WHERE status IN ('pending', 'running', 'paused_awaiting_reply');
-- mig 020: at most one running session at a time.
CREATE UNIQUE INDEX IF NOT EXISTS uq_research_sessions_single_running
    ON research_sessions(status) WHERE status = 'running';

-- mig 011 (+016 timezone, +018 status check): scheduled research jobs.
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id              SERIAL PRIMARY KEY,
    topic           TEXT NOT NULL,
    depth           TEXT NOT NULL DEFAULT 'medium'
        CHECK (depth IN ('shallow', 'medium', 'deep')),
    cron_expression TEXT NOT NULL,
    enabled         BOOLEAN NOT NULL DEFAULT TRUE,
    last_run_at     TIMESTAMPTZ,
    last_status     TEXT
        CHECK (last_status IS NULL OR last_status IN ('success', 'failed', 'running', 'timeout')),
    last_job_id     TEXT,
    next_run_at     TIMESTAMPTZ,
    run_count       INTEGER NOT NULL DEFAULT 0,
    failure_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    timezone        VARCHAR(64) NOT NULL DEFAULT 'UTC'
);
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_enabled ON scheduled_jobs(enabled) WHERE enabled = TRUE;
CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_next_run ON scheduled_jobs(next_run_at) WHERE enabled = TRUE;

-- mig 030: per-provider/model cost rates.
CREATE TABLE IF NOT EXISTS model_costs (
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    input_per_1m_usd  NUMERIC(10,6) NOT NULL DEFAULT 0,
    output_per_1m_usd NUMERIC(10,6) NOT NULL DEFAULT 0,
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (provider, model)
);

-- mig 030 (+033 call_kind): per-call LLM telemetry.
CREATE TABLE IF NOT EXISTS llm_call_logs (
    id                BIGSERIAL PRIMARY KEY,
    job_id            UUID,
    node_id           UUID,
    provider          TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    cost_usd          NUMERIC(10,6) NOT NULL DEFAULT 0,
    success           BOOLEAN NOT NULL DEFAULT TRUE,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    call_kind         TEXT
);
CREATE INDEX IF NOT EXISTS idx_llm_call_logs_call_kind ON llm_call_logs(call_kind) WHERE call_kind IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_llm_call_logs_created_at ON llm_call_logs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_llm_call_logs_job_id ON llm_call_logs(job_id) WHERE job_id IS NOT NULL;

-- mig 032: system alert sink.
CREATE TABLE IF NOT EXISTS system_alerts (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    kind       TEXT NOT NULL,
    severity   TEXT NOT NULL
        CHECK (severity IN ('info', 'warning', 'critical')),
    message    TEXT NOT NULL,
    payload    JSONB NOT NULL DEFAULT '{}'::jsonb,
    dedup_key  TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_system_alerts_created_at ON system_alerts(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_system_alerts_dedup_key_created
    ON system_alerts(dedup_key, created_at DESC) WHERE dedup_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_system_alerts_kind_created ON system_alerts(kind, created_at DESC);

-- mig 022: per-node prompt revision history.
CREATE TABLE IF NOT EXISTS prompt_revisions (
    id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id          UUID NOT NULL,
    node_key        TEXT NOT NULL,
    revision_number INTEGER NOT NULL,
    prompt_text     TEXT NOT NULL,
    edited_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    edited_by       TEXT,
    source          TEXT NOT NULL DEFAULT 'manual'
        CHECK (source IN ('manual', 'optimizer', 'initial', 'system')),
    CONSTRAINT prompt_revisions_unique_revision UNIQUE (job_id, node_key, revision_number),
    CONSTRAINT prompt_revisions_job_node_fkey
        FOREIGN KEY (job_id, node_key) REFERENCES dag_nodes(job_id, node_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_prompt_revisions_edited_at ON prompt_revisions(edited_at DESC);
CREATE INDEX IF NOT EXISTS idx_prompt_revisions_job_node
    ON prompt_revisions(job_id, node_key, revision_number DESC);

-- mig 023 (+024 drop applied_status): Assist Mode session.
CREATE TABLE IF NOT EXISTS assist_sessions (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id           UUID NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE CASCADE,
    status           TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'completed', 'abandoned', 'cancelled')),
    current_node_key TEXT,
    handoff_policy   TEXT NOT NULL DEFAULT 'manual'
        CHECK (handoff_policy IN ('manual', 'auto_on_skip', 'auto_all_remaining')),
    replan_policy    TEXT NOT NULL DEFAULT 'context_only'
        CHECK (replan_policy IN ('context_only', 'selective', 'full', 'disabled')),
    started_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at     TIMESTAMPTZ,
    metadata         JSONB NOT NULL DEFAULT '{}'::jsonb,
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_assist_sessions_status ON assist_sessions(status);
CREATE INDEX IF NOT EXISTS idx_assist_sessions_last_activity
    ON assist_sessions(last_activity_at) WHERE status IN ('active', 'paused');

-- mig 023: Assist Mode per-step walker state. The guidance_* columns are added
-- on top by mig 051 (which the runner applies after this baseline is seeded).
CREATE TABLE IF NOT EXISTS assist_steps (
    id               UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id       UUID NOT NULL REFERENCES assist_sessions(id) ON DELETE CASCADE,
    job_id           UUID NOT NULL,
    node_key         TEXT NOT NULL,
    status           TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'presented', 'awaiting_input', 'received',
                          'committed', 'skipped', 'escalated', 'handed_off')),
    presented_at     TIMESTAMPTZ,
    submitted_at     TIMESTAMPTZ,
    committed_at     TIMESTAMPTZ,
    evidence_kind    TEXT
        CHECK (evidence_kind IS NULL OR evidence_kind IN ('text', 'command_output',
              'file_diff', 'screenshot_ref', 'url', 'none')),
    evidence         TEXT,
    evidence_meta    JSONB NOT NULL DEFAULT '{}'::jsonb,
    friction_note    TEXT,
    divergence       BOOLEAN NOT NULL DEFAULT FALSE,
    replan_triggered BOOLEAN NOT NULL DEFAULT FALSE,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT assist_steps_session_id_node_key_key UNIQUE (session_id, node_key),
    CONSTRAINT assist_steps_job_node_fkey
        FOREIGN KEY (job_id, node_key) REFERENCES dag_nodes(job_id, node_key) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_assist_steps_job_node ON assist_steps(job_id, node_key);
CREATE INDEX IF NOT EXISTS idx_assist_steps_session_status ON assist_steps(session_id, status);

-- mig 021: updated_at triggers for the non-core tables above.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_research_sessions_updated_at') THEN
        CREATE TRIGGER trg_research_sessions_updated_at BEFORE UPDATE ON research_sessions
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_scheduled_jobs_updated_at') THEN
        CREATE TRIGGER trg_scheduled_jobs_updated_at BEFORE UPDATE ON scheduled_jobs
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_assist_sessions_updated_at') THEN
        CREATE TRIGGER trg_assist_sessions_updated_at BEFORE UPDATE ON assist_sessions
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'trg_assist_steps_updated_at') THEN
        CREATE TRIGGER trg_assist_steps_updated_at BEFORE UPDATE ON assist_steps
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
