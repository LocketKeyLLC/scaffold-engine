-- 065_model_role_proposals.sql
-- §17.803 — role→model learning: staged swap proposals for human review.
--
-- A periodic golden re-A/B job (app/modules/model_role_learning) scores
-- candidate models per switchable role on the phase-1 goldens and, when a
-- candidate beats the incumbent clean, stages an `open` proposal here. The
-- operator reviews it as a §17.629 confirm card in chat; accepting applies the
-- swap via app.modules.model_overrides.set_override. NOTHING auto-swaps — a row
-- is a suggestion until an explicit human confirm flips it to `accepted`.
--
-- Single statement: the whole thing is one `DO $$ … $$` block (per §17.140 —
-- the asyncpg prepared-statement runner path chokes silently on multi-`;`
-- files, so table + indexes are created via EXECUTE inside one DO, each guarded
-- IF NOT EXISTS so re-apply is a no-op). Mirrors migration 030's shape.
DO $$
BEGIN
    -- ── 1. model_role_proposals table ─────────────────────────────────────
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = current_schema()
           AND table_name   = 'model_role_proposals'
    ) THEN
        EXECUTE 'CREATE TABLE model_role_proposals (
            id               BIGSERIAL PRIMARY KEY,
            role             TEXT NOT NULL,
            task             TEXT NOT NULL,
            incumbent_model  TEXT NOT NULL,
            candidate_model  TEXT NOT NULL,
            incumbent_rate   DOUBLE PRECISION NOT NULL DEFAULT 0,
            candidate_rate   DOUBLE PRECISION NOT NULL DEFAULT 0,
            speedup          DOUBLE PRECISION NOT NULL DEFAULT 0,
            evidence         JSONB,
            status           TEXT NOT NULL DEFAULT ''open'',
            created_at       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            decided_at       TIMESTAMPTZ
        )';
    END IF;

    -- ── 2. At most one OPEN proposal per role ─────────────────────────────
    -- Partial UNIQUE on role WHERE status='open' — a new learning cycle first
    -- supersedes the prior open row (status→'superseded') then INSERTs, so this
    -- index never blocks the fresh proposal; it guards against a double-insert
    -- race (two cycles overlapping) leaving two live proposals for one role.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = current_schema()
           AND indexname  = 'idx_model_role_proposals_open_role'
    ) THEN
        EXECUTE 'CREATE UNIQUE INDEX idx_model_role_proposals_open_role
            ON model_role_proposals(role)
            WHERE status = ''open''';
    END IF;

    -- ── 3. Status lookup (list open / audit accepted) ─────────────────────
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = current_schema()
           AND indexname  = 'idx_model_role_proposals_status'
    ) THEN
        EXECUTE 'CREATE INDEX idx_model_role_proposals_status
            ON model_role_proposals(status)';
    END IF;
END $$;
