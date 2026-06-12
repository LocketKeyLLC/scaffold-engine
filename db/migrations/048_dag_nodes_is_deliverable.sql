-- 048_dag_nodes_is_deliverable.sql
-- §17.475 Phase 1 — explicit deliverable role.
--
-- Add a model-asserted `is_deliverable` flag set by the DAG generator at
-- INSERT time. This becomes the PRIMARY signal for what the compiled
-- deliverable is built from, fixing the §17.471-474 class where a dead-end
-- branch leaf was treated as a co-deliverable.
--
-- Backward-compat: NO backfill. Existing rows default FALSE, so jobs created
-- before this migration carry no explicit deliverable and `_compile_output`
-- falls back to the unchanged is_output_node + dominant-leaf (§17.473) path —
-- bit-identical behavior for old jobs. `is_output_node` is retained as the
-- fallback signal and is NOT superseded.
--
-- ONE statement (a single DO block) per the asyncpg "no multiple commands in a
-- prepared statement" rule (§17.140); the ALTER + COMMENT run inside it.

DO $mig$
BEGIN
    ALTER TABLE dag_nodes
        ADD COLUMN IF NOT EXISTS is_deliverable BOOLEAN NOT NULL DEFAULT FALSE;

    COMMENT ON COLUMN dag_nodes.is_deliverable IS
        'Model-asserted final-deliverable node (§17.475 Phase 1). Primary signal for compile Strategy 0; is_output_node remains the topological-leaf fallback.';
END
$mig$;
