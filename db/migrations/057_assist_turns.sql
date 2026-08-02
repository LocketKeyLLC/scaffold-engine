-- 057_assist_turns.sql
-- §17.710a — unified session memory, Stage A: an append-only raw transcript of
-- every operator/engine turn in an Assist session, captured UNCONDITIONALLY at
-- the endpoint BEFORE any intent classification runs. This is the lossless
-- capture layer that the narrow, trigger-specific retention channels
-- (placeholder substitution-learning §17.490, exec-context regex §17.703, the
-- note classifier §17.677, the facts distiller §17.709) each missed: whenever a
-- turn didn't match a channel's exact trigger — an audit paste with no
-- placeholders, a message the LLM mislabeled `question` — the information was
-- never even recorded. Stage B derives the consolidated session_memory FROM
-- these rows (so a missed distill is always recoverable from the transcript).
--
-- ONE statement (single DO block) per the asyncpg "no multiple commands in a
-- prepared statement" rule (§17.140; cf. 049_dag_node_edits). All additive /
-- IF NOT EXISTS — safe to re-run; no backfill.
DO $mig$
BEGIN
    CREATE TABLE IF NOT EXISTS assist_turns (
        id            BIGSERIAL PRIMARY KEY,
        session_id    UUID NOT NULL,
        job_id        UUID,
        node_key      TEXT,
        role          TEXT NOT NULL,     -- 'operator' | 'engine'
        kind          TEXT NOT NULL,     -- submit | message | skip | note | guidance | decision
        content       TEXT NOT NULL DEFAULT '',
        evidence_kind TEXT,
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    -- Read path is always "this session's turns, in order" (consolidation +
    -- the /turns roll-up), so index (session_id, created_at, id).
    CREATE INDEX IF NOT EXISTS idx_assist_turns_session
        ON assist_turns (session_id, created_at, id);

    -- ON DELETE CASCADE so turns vanish with their session (and, transitively,
    -- when a job is deleted: jobs → assist_sessions CASCADEs → assist_turns).
    -- Drop-then-add (Postgres has no ADD CONSTRAINT IF NOT EXISTS) keeps this
    -- idempotent whether the table pre-exists or is freshly created here.
    ALTER TABLE assist_turns DROP CONSTRAINT IF EXISTS assist_turns_session_fk;
    ALTER TABLE assist_turns
        ADD CONSTRAINT assist_turns_session_fk
        FOREIGN KEY (session_id) REFERENCES assist_sessions(id) ON DELETE CASCADE;

    COMMENT ON TABLE assist_turns IS
        'Append-only raw transcript of Assist Mode turns (§17.710a). Captured before classification so retention never depends on the LLM getting intent right; session_memory (Stage B) is derived from these rows.';
END
$mig$;
