-- Migration 020: Atomic single-running guard for research_sessions
-- Replaces TOCTOU _guard_concurrent SELECT + separate INSERT with
-- an atomic INSERT that raises UniqueViolation if another session
-- is already in 'running' state.
--
-- Only one row with status='running' may exist at any time.
-- 'paused_awaiting_reply' is NOT guarded here — resume flow handles
-- its own single-claimant semantics via _atomic_claim_for_resume.

-- Defensive: drop any leftover 'running' rows older than the 30-min reaper
-- window so the partial unique index can be created cleanly.
UPDATE research_sessions
   SET status = 'cancelled',
       error_message = COALESCE(error_message, 'reaped_before_020'),
       completed_at = now(),
       updated_at = now()
 WHERE status = 'running'
   AND updated_at < now() - interval '30 minutes';

CREATE UNIQUE INDEX IF NOT EXISTS uq_research_sessions_single_running
    ON research_sessions ((status))
 WHERE status = 'running';

COMMENT ON INDEX uq_research_sessions_single_running IS
'Enforces singleton running session. Replaces TOCTOU guard in research_agent._guard_concurrent.';
