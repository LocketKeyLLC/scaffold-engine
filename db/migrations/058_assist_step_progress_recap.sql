-- §17.738 — per-step running "progress recap" so long, multi-sub-problem
-- troubleshooting steps stay coherent. The fix/guide/research paths only ever
-- saw a 6-turn conversation window (§17.687); on a 37-turn marathon step that's
-- ~16% of the thread, so the engine "lost its place" (re-suggested resolved
-- fixes, forgot which machine commands run on, needed the operator to
-- re-orient it). This recap is derived from the FULL node-scoped assist_turns
-- transcript (DB-backed, so it survives a restart too), cached here, and
-- refreshed only when the step's turn count grows past the watermark.
-- Single statement (comma-separated ALTER) per the asyncpg migration invariant.
ALTER TABLE assist_steps
    ADD COLUMN IF NOT EXISTS progress_recap TEXT,
    ADD COLUMN IF NOT EXISTS progress_recap_turns INTEGER NOT NULL DEFAULT 0;
