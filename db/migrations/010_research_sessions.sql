-- Migration 010: Research session tracking
CREATE TABLE IF NOT EXISTS research_sessions (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    topic           TEXT NOT NULL,
    depth           VARCHAR(10) NOT NULL DEFAULT 'medium',
    domain          VARCHAR(50) NOT NULL DEFAULT 'eng',
    iterations_completed    INT NOT NULL DEFAULT 0,
    total_entries_extracted  INT NOT NULL DEFAULT 0,
    total_entries_ingested   INT NOT NULL DEFAULT 0,
    total_entries_rejected   INT NOT NULL DEFAULT 0,
    total_urls_searched      INT NOT NULL DEFAULT 0,
    total_queries            INT NOT NULL DEFAULT 0,
    duration_ms     INT NOT NULL DEFAULT 0,
    coverage_pct    FLOAT,
    status          VARCHAR(20) NOT NULL DEFAULT 'running',
    summary         TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ
);

CREATE INDEX idx_research_sessions_status ON research_sessions(status);
CREATE INDEX idx_research_sessions_created_at ON research_sessions(created_at DESC);
