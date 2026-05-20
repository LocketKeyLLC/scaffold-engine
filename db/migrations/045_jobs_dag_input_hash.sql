-- 045_jobs_dag_input_hash.sql
-- §17.181 — Add a hash of the inputs the DAG was generated from so the
-- /dag re-entry guard can distinguish "idempotent retry" (same brief,
-- same overrides, same model — return 409) from "drift" (brief changed
-- since last generation — log + recompute when no execution started).
--
-- Pre-§17.181 the re-entry guard was a node-count check: any existing
-- dag_nodes row → 409. That works for the current contract (briefs are
-- immutable on awaiting_confirmation) but silently mis-handles any
-- future "edit brief and re-confirm" flow where the user expects the
-- DAG to recompute against the new brief.
--
-- The column is nullable so all pre-§17.181 jobs keep working: a NULL
-- hash means "we don't know what inputs produced these nodes" and the
-- generator falls back to the legacy count-only rejection in that case.
-- DO-block wrapped per the asyncpg multi-statement rule (§17.140).

DO $$
BEGIN
    ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS dag_input_hash TEXT;
END $$;
