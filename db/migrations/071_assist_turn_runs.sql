-- §17.869 — durable assist turn runs: the turn loop (§17.868) runs as a
-- server-side background task appending every SSE frame here; clients TAIL
-- the row. A browser reload resumes the tail — it can no longer kill the
-- turn mid-flight (the live failure: multi-minute turns died when the
-- operator reloaded during a slow stage). Single statement (runner rule).
CREATE TABLE IF NOT EXISTS assist_turn_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id UUID NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    frames JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at TIMESTAMPTZ
)
