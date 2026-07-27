-- §17.654 — session-level "notes & additions" capture for Assist Mode.
-- New/important things an operator raises mid-conversation (a fresh
-- requirement, a constraint, a decision made out of band) are appended here so
-- they (a) survive the step they were raised on, (b) feed forward into later
-- steps' guidance context, and (c) surface in the /results rollup. One row per
-- session; the notes list is a JSONB array of {ts, kind, node_key, text}.
-- Single-statement, idempotent (migration runner is prepared-statement path).
ALTER TABLE assist_sessions ADD COLUMN IF NOT EXISTS notes JSONB NOT NULL DEFAULT '[]'::jsonb;
