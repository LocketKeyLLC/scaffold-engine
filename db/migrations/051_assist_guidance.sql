-- Migration 051: Assist Mode guidance cache (§17.486)
-- (Renumbered from 025 in §17.487 — 025 collided with the pre-existing
--  025_drop_dead_error_types.sql; migrations run to 050. Already applied to
--  the live DB under the old name; the runner re-applies this idempotently.)
--
-- Assist Mode now generates a human-facing walkthrough per step (copy-paste
-- terminal commands for shell/codegen work, step-by-step instructions for
-- non-coding work), optionally grounded in a SearXNG/Milvus research pre-pass.
-- The generated guidance is cached on the owning assist_steps row so that
-- re-viewing a step (or a re-`/assist next` that re-claims it) does not
-- re-spend an LLM call. `/assist guide` regenerates with force=true.
--
-- Columns:
--   guidance              — the rendered walkthrough markdown (NULL = not generated)
--   guidance_meta         — {model, research_sources:[{query,kind}], generated_at,
--                            tool, refine_hint, status}
--   guidance_status       — none | generating | ready | failed
--   guidance_generated_at — wall-clock of the last successful generation
--
-- ONE statement on purpose: the runner (app/migrations.py) executes each
-- file through asyncpg's prepared-statement path, which rejects multiple
-- semicolon-separated commands ("cannot insert multiple commands into a
-- prepared statement"). A single ALTER TABLE with comma-separated actions
-- sidesteps that. Idempotent: ADD COLUMN IF NOT EXISTS skips columns that
-- already exist (including the inline CHECK), so re-applying is a no-op. The
-- 023 update_updated_at trigger still fires on UPDATEs that write these.

ALTER TABLE assist_steps
    ADD COLUMN IF NOT EXISTS guidance TEXT,
    ADD COLUMN IF NOT EXISTS guidance_meta JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS guidance_status TEXT NOT NULL DEFAULT 'none'
        CHECK (guidance_status IN ('none', 'generating', 'ready', 'failed')),
    ADD COLUMN IF NOT EXISTS guidance_generated_at TIMESTAMPTZ;
