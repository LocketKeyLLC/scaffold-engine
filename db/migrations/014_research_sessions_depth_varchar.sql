-- Migration 014: Widen research_sessions.depth to VARCHAR(32)
-- Rationale: 'direct_pdf' is 10 chars (current cap = 10). Future modes
-- (direct_openapi, direct_github, etc.) will overflow VARCHAR(10).
-- Date: April 18, 2026

BEGIN;

ALTER TABLE research_sessions
    ALTER COLUMN depth TYPE VARCHAR(32);

COMMIT;
