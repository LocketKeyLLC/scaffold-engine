-- 049_dag_node_edits.sql
-- §17.478 Phase 4 — interactive node control (CRUD).
--
-- Adds an optimistic-lock version on dag_nodes (there is no existing
-- concurrency guard to reuse) and an append-only edit-audit table mirroring
-- the prompt_revisions (mig 022) shape. Every node_editor mutation reads the
-- caller's expected edit_version (409 on mismatch), bumps it on success, and
-- writes a dag_node_edits row in the same transaction.
--
-- ONE statement (single DO block) per the asyncpg "no multiple commands in a
-- prepared statement" rule (§17.140). All additive / IF NOT EXISTS — safe to
-- re-run; no backfill (edit_version defaults 0 for existing rows).

DO $mig$
BEGIN
    ALTER TABLE dag_nodes
        ADD COLUMN IF NOT EXISTS edit_version INTEGER NOT NULL DEFAULT 0;

    CREATE TABLE IF NOT EXISTS dag_node_edits (
        id          BIGSERIAL PRIMARY KEY,
        job_id      UUID NOT NULL,
        node_key    TEXT NOT NULL,
        op          TEXT NOT NULL,        -- edit | insert | delete | reorder | reset
        before      JSONB,
        after       JSONB,
        edited_by   TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    CREATE INDEX IF NOT EXISTS idx_dag_node_edits_job
        ON dag_node_edits (job_id, created_at DESC);

    COMMENT ON COLUMN dag_nodes.edit_version IS
        'Optimistic-lock version (§17.478). node_editor bumps it on every mutation; a stale expected_version => HTTP 409.';
END
$mig$;
