-- 062_jobs_cost_budget.sql
-- §17.777 — hard per-job cost/token budgets.
--
-- Sprint J.3 already tallies every LLM call into `llm_call_logs` (tokens +
-- USD cost, tagged by job_id) and `cost_rollup.get_job_cost_totals` sums it.
-- What was missing is ENFORCEMENT: nothing read those totals to stop a job.
-- These two nullable per-job override columns let an operator cap a single
-- job. NULL = inherit the settings default (cost_budget_default_max_tokens /
-- cost_budget_default_max_usd); 0 on either means "unlimited for this axis".
--
--   token_budget     — max total tokens (prompt + completion) this job may
--                      spend across all LLM calls before it is hard-stopped.
--   cost_budget_usd  — max USD this job may spend. Only bites when the
--                      relevant models have `model_costs` rows (local/:cloud
--                      Ollama tags are unpriced → $0, so the token cap is the
--                      lever that bites on the default all-Ollama deployment).
--
-- Enforcement is at the node boundary (execute_next_node Phase 1): once the
-- accumulated spend from already-executed nodes exceeds either cap, the next
-- node does not start and the job is marked 'failed' with
-- error_summary 'cost_budget_exhausted' (mirrors the §17.774 crash-loop
-- guard's terminal-write pattern). The whole feature is gated behind the
-- default-OFF master valve settings.cost_budget_enforcement_enabled, so this
-- migration is a no-op on behavior until an operator opts in.
--
-- ONE statement (single DO block) per the asyncpg "no multiple commands in a
-- prepared statement" rule (§17.140). All additive / IF NOT EXISTS — safe to
-- re-run; existing rows leave both columns NULL (inherit settings default).

DO $mig$
BEGIN
    ALTER TABLE jobs
        ADD COLUMN IF NOT EXISTS token_budget    INTEGER,
        ADD COLUMN IF NOT EXISTS cost_budget_usd NUMERIC;

    COMMENT ON COLUMN jobs.token_budget IS
        'Per-job max total tokens before hard-stop (§17.777). NULL = inherit settings.cost_budget_default_max_tokens; 0 = unlimited.';
    COMMENT ON COLUMN jobs.cost_budget_usd IS
        'Per-job max USD spend before hard-stop (§17.777). NULL = inherit settings.cost_budget_default_max_usd; 0 = unlimited.';
END
$mig$;
