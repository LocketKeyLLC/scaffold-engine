-- Migration 023: Assistant Mode (assist_sessions + assist_steps)
-- Adds the human-in-the-loop "walk through DAG steps" subsystem.
-- See docs/ARCHITECTURE.md "Assistant Mode" and references/assist.md.
--
-- Idempotent: re-running is a no-op.

BEGIN;

-- ── 1. Widen jobs.status CHECK to include assisted_* statuses ──────────────
-- Same DO-block pattern as migrations 004 and 008.
DO $$
DECLARE
    constraint_def TEXT;
BEGIN
    SELECT pg_get_constraintdef(oid) INTO constraint_def
    FROM pg_constraint
    WHERE conrelid = 'jobs'::regclass
      AND contype = 'c'
      AND conname LIKE '%status%';

    IF constraint_def IS NOT NULL AND (
        constraint_def NOT LIKE '%assisted_executing%'
        OR constraint_def NOT LIKE '%assisted_running%'
        OR constraint_def NOT LIKE '%assisted_paused%'
    ) THEN
        EXECUTE 'ALTER TABLE jobs DROP CONSTRAINT ' ||
            (SELECT conname FROM pg_constraint
             WHERE conrelid = 'jobs'::regclass
               AND contype = 'c'
               AND conname LIKE '%status%');
        ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
            CHECK (status IN (
                'pending', 'refining', 'planning', 'executing', 'running',
                'completed', 'failed', 'cancelled', 'blocked',
                'awaiting_confirmation', 'researching',
                'assisted_executing', 'assisted_running', 'assisted_paused'
            ));
    ELSIF constraint_def IS NULL THEN
        ALTER TABLE jobs ADD CONSTRAINT jobs_status_check
            CHECK (status IN (
                'pending', 'refining', 'planning', 'executing', 'running',
                'completed', 'failed', 'cancelled', 'blocked',
                'awaiting_confirmation', 'researching',
                'assisted_executing', 'assisted_running', 'assisted_paused'
            ));
    END IF;
END;
$$;

-- ── 2. assist_sessions ─────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS assist_sessions (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    job_id UUID NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'active'
        CHECK (status IN ('active', 'paused', 'completed', 'abandoned', 'cancelled')),
    current_node_key TEXT,
    handoff_policy TEXT NOT NULL DEFAULT 'manual'
        CHECK (handoff_policy IN ('manual', 'auto_on_skip', 'auto_all_remaining')),
    replan_policy TEXT NOT NULL DEFAULT 'context_only'
        CHECK (replan_policy IN ('context_only', 'selective', 'full', 'disabled')),
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_activity_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    UNIQUE (job_id)
);

-- Backfill the column if the table predates this migration (e.g. an
-- older version of 023 ran first). Idempotent.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_name = 'assist_sessions' AND column_name = 'updated_at'
    ) THEN
        ALTER TABLE assist_sessions
            ADD COLUMN updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW();
    END IF;
END;
$$;

CREATE INDEX IF NOT EXISTS idx_assist_sessions_status
    ON assist_sessions (status);
CREATE INDEX IF NOT EXISTS idx_assist_sessions_last_activity
    ON assist_sessions (last_activity_at)
    WHERE status IN ('active', 'paused');

-- ── 3. assist_steps ────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS assist_steps (
    id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    session_id UUID NOT NULL REFERENCES assist_sessions(id) ON DELETE CASCADE,
    job_id UUID NOT NULL,
    node_key TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending', 'presented', 'awaiting_input', 'received',
            'applied', 'committed', 'skipped', 'escalated', 'handed_off'
        )),
    presented_at TIMESTAMPTZ,
    submitted_at TIMESTAMPTZ,
    committed_at TIMESTAMPTZ,
    evidence_kind TEXT
        CHECK (evidence_kind IS NULL OR evidence_kind IN
            ('text', 'command_output', 'file_diff', 'screenshot_ref', 'url', 'none')),
    evidence TEXT,
    evidence_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    friction_note TEXT,
    divergence BOOLEAN NOT NULL DEFAULT FALSE,
    replan_triggered BOOLEAN NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (session_id, node_key),
    CONSTRAINT assist_steps_job_node_fkey
        FOREIGN KEY (job_id, node_key) REFERENCES dag_nodes (job_id, node_key)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_assist_steps_session_status
    ON assist_steps (session_id, status);
CREATE INDEX IF NOT EXISTS idx_assist_steps_job_node
    ON assist_steps (job_id, node_key);

-- ── 4. Reuse existing update_updated_at trigger function (init.sql:170) ────
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_assist_sessions_updated_at'
    ) THEN
        CREATE TRIGGER trg_assist_sessions_updated_at
            BEFORE UPDATE ON assist_sessions
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_assist_steps_updated_at'
    ) THEN
        CREATE TRIGGER trg_assist_steps_updated_at
            BEFORE UPDATE ON assist_steps
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;
END;
$$;

COMMIT;
