-- Migration 022: prompt_revisions
-- Adds an audit trail for DAG node prompt edits (audit items #7.8, #7.9).
-- Each call to update_prompt() writes the *previous* prompt as a revision row
-- before applying the new value, so the chain of edits is preserved.

BEGIN;

CREATE TABLE IF NOT EXISTS prompt_revisions (
    id              uuid        PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id          uuid        NOT NULL,
    node_key        text        NOT NULL,
    revision_number integer     NOT NULL,
    prompt_text     text        NOT NULL,
    edited_at       timestamptz NOT NULL DEFAULT now(),
    edited_by       text                 DEFAULT NULL,
    source          text        NOT NULL DEFAULT 'manual',
    CONSTRAINT prompt_revisions_source_check
        CHECK (source IN ('manual', 'optimizer', 'initial', 'system')),
    CONSTRAINT prompt_revisions_job_node_fkey
        FOREIGN KEY (job_id, node_key)
        REFERENCES dag_nodes (job_id, node_key)
        ON DELETE CASCADE,
    CONSTRAINT prompt_revisions_unique_revision
        UNIQUE (job_id, node_key, revision_number)
);

CREATE INDEX IF NOT EXISTS idx_prompt_revisions_job_node
    ON prompt_revisions (job_id, node_key, revision_number DESC);

CREATE INDEX IF NOT EXISTS idx_prompt_revisions_edited_at
    ON prompt_revisions (edited_at DESC);

COMMIT;
