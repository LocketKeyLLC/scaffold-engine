-- Migration 012: Extend research_sessions for resumable/pausable research
-- Phase 1: adds state_snapshot (rehydration payload), updated_at (reaper), error_message (failure context)

ALTER TABLE research_sessions
    ADD COLUMN state_snapshot JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN updated_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    ADD COLUMN error_message  TEXT;

-- Backfill updated_at for pre-existing rows from their last known timestamp
UPDATE research_sessions
SET updated_at = COALESCE(completed_at, created_at);

-- Partial index: reaper scans active rows only; stays tiny as completed rows accumulate
CREATE INDEX idx_research_sessions_active_updated
    ON research_sessions(status, updated_at DESC)
    WHERE status IN ('pending', 'running');
