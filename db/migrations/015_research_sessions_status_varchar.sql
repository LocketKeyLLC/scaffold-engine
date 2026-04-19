-- Migration 015: Widen research_sessions.status to VARCHAR(32)
-- Fixes pre-existing bug from migration 013: paused_awaiting_reply is 21
-- chars but status column was still VARCHAR(20). Pause writes succeeded in
-- tests because tests mocked the DB; real inserts throw
-- StringDataRightTruncationError.
-- Date: April 18, 2026

BEGIN;

ALTER TABLE research_sessions
    ALTER COLUMN status TYPE VARCHAR(32);

COMMIT;
