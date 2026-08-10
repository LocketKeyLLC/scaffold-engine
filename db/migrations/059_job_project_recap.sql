-- §17.753 — the cross-step "living project recap" (§17.679 deferred item). The
-- per-step recap (§17.738, migration 058) keeps ONE step coherent; the job digest
-- (§17.650) dumps raw done-node outputs. Neither is a distilled, EVOLVING view of
-- where the WHOLE build stands across steps — so guidance/pivot on step N was blind
-- to the arc (what earlier steps decided, what remains, cross-step constraints).
-- This caches a distilled whole-project state board on the job, refreshed only when
-- the count of DONE nodes grows past the watermark (so it costs ~one LLM call per
-- completed step, cached across the many turns within a step). It's the project
-- analog of assist_steps.progress_recap.
-- Single statement (comma-separated ALTER) per the asyncpg migration invariant.
ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS project_recap TEXT,
    ADD COLUMN IF NOT EXISTS project_recap_nodes INTEGER NOT NULL DEFAULT 0;
