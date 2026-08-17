-- 067_runtime_profile.sql
-- §17.809 — runtime compute profiles ("quick mode" GPU/cloud-fast preset).
--
-- Two surfaces share this migration:
--
--   1. runtime_profile — a SINGLETON row (id = TRUE, CHECK-pinned) naming the
--      globally-active profile plus the exact settings snapshot that was applied
--      when it was activated. The per-role MODEL swaps a profile makes ride on
--      the existing `model_overrides` table (migration 050) so they reload for
--      free; this table carries only the profile NAME + the non-model knob
--      snapshot (max_retries, node_escalation, faithfulness/CoVe, research caps)
--      so `clear_profile` can revert each knob to its precise pre-activation
--      value and startup can re-apply them. Empty until a profile is activated
--      via POST /config/profile.
--
--   2. jobs.quick_mode — a per-JOB opt-in (the `/go … --quick` flag). When TRUE
--      the orchestrator layers the quick profile's MODEL map under that job's
--      request overrides across every phase and forces research depth shallow,
--      WITHOUT touching global settings (so a `--quick` job doesn't slow or
--      speed up anyone else). Knob-tightening is global-profile-only.
--
-- Single statement: table + column + index live inside one `DO $$ … $$` block
-- (§17.140 — the asyncpg prepared-statement runner path chokes silently on
-- multi-`;` files), each guarded IF NOT EXISTS so re-apply is a no-op. Mirrors
-- migrations 065/066.
DO $$
BEGIN
    -- ── 1. runtime_profile singleton ──────────────────────────────────────
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = current_schema()
           AND table_name   = 'runtime_profile'
    ) THEN
        EXECUTE 'CREATE TABLE runtime_profile (
            id                BOOLEAN     PRIMARY KEY DEFAULT TRUE,
            name              TEXT        NOT NULL,
            applied_settings  JSONB       NOT NULL DEFAULT ''{}''::jsonb,
            updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT runtime_profile_singleton CHECK (id)
        )';
    END IF;

    -- ── 2. per-job quick-mode flag ────────────────────────────────────────
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name   = 'jobs'
           AND column_name  = 'quick_mode'
    ) THEN
        EXECUTE 'ALTER TABLE jobs ADD COLUMN quick_mode BOOLEAN NOT NULL DEFAULT FALSE';
    END IF;

    -- ── 3. partial index over the (rare) quick jobs ───────────────────────
    -- Phase entry points probe `SELECT quick_mode FROM jobs WHERE id = $1`
    -- alongside other columns, so no dedicated index is needed for the point
    -- read; this partial index only helps operational "which jobs ran quick"
    -- rollups without bloating the common case.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = current_schema()
           AND indexname  = 'idx_jobs_quick_mode'
    ) THEN
        EXECUTE 'CREATE INDEX idx_jobs_quick_mode ON jobs (quick_mode) WHERE quick_mode';
    END IF;
END $$;
