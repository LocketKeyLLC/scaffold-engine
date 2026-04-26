-- Migration 013: Phase 2 pause mechanics for resumable research.
-- Adds pause_question (prompt shown to user), pause_expires_at (1h timeout),
-- pause_reply (user's answer). Extends active-sessions index to cover
-- paused_awaiting_reply so the reaper sees these rows.
ALTER TABLE research_sessions
    ADD COLUMN pause_question   TEXT,
    ADD COLUMN pause_expires_at TIMESTAMPTZ,
    ADD COLUMN pause_reply      TEXT;
-- Rebuild the active-sessions partial index to include the new status.
-- Wrapped in a transaction so the rebuild is atomic: if CREATE fails, the
-- DROP rolls back and the prior index survives.
BEGIN;
DROP INDEX IF EXISTS idx_research_sessions_active_updated;
CREATE INDEX IF NOT EXISTS idx_research_sessions_active_updated
    ON research_sessions(status, updated_at DESC)
    WHERE status IN ('pending', 'running', 'paused_awaiting_reply');
COMMIT;
