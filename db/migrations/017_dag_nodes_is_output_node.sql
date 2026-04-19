-- 016_dag_nodes_is_output_node.sql
-- Add explicit is_output_node flag on dag_nodes.
-- Fix list item #97: replace fragile title-heuristic in _compile_output
-- with an explicit marker set by the DAG generator at INSERT time.
--
-- Backfill: mark current leaves (nodes that no other node depends on)
-- as output nodes so existing jobs benefit from the explicit path too.

ALTER TABLE dag_nodes
    ADD COLUMN IF NOT EXISTS is_output_node BOOLEAN NOT NULL DEFAULT FALSE;

-- Backfill existing rows: a node is a leaf if no sibling in the same
-- job lists it in depends_on.
UPDATE dag_nodes AS dn
SET is_output_node = TRUE
WHERE NOT EXISTS (
    SELECT 1 FROM dag_nodes d2
    WHERE d2.job_id = dn.job_id
      AND dn.node_key = ANY(d2.depends_on)
);

CREATE INDEX IF NOT EXISTS idx_dag_nodes_output_node
    ON dag_nodes (job_id, is_output_node)
    WHERE is_output_node = TRUE;
