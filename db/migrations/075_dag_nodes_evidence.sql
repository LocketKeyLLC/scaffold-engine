-- §17.1040 — the executor's per-node evidence report (§17.1039: unsupported
-- values, plan-only values, command-shape issues, citation score, whether the
-- output was regenerated) lives on the node row so /exec/status, the SPA Run
-- tab and the CLI can show it, and downstream nodes can read which upstream
-- values were flagged. NULL = the node ran before this column existed or the
-- check was off. Single statement (asyncpg runner).
ALTER TABLE dag_nodes ADD COLUMN IF NOT EXISTS evidence JSONB
