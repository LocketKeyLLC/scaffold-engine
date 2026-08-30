-- §17.869 — active-run lookup on session load (resume-after-reload).
CREATE INDEX IF NOT EXISTS idx_assist_turn_runs_session ON assist_turn_runs (session_id, created_at DESC)
