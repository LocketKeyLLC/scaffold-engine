-- 019: Ensure UNIQUE (job_id, node_key) on dag_nodes.
-- On inspection, constraint `dag_nodes_job_id_node_key_key` was already
-- present from original table creation. This migration is a documented
-- safety net: it verifies the constraint exists and adds it only if
-- missing (which would indicate schema drift).
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.table_constraints
        WHERE table_name = 'dag_nodes'
          AND constraint_type = 'UNIQUE'
          AND constraint_name IN (
              'dag_nodes_job_id_node_key_key',
              'dag_nodes_job_id_node_key_unique'
          )
    ) THEN
        ALTER TABLE dag_nodes
            ADD CONSTRAINT dag_nodes_job_id_node_key_unique
            UNIQUE (job_id, node_key);
        RAISE NOTICE 'Added dag_nodes_job_id_node_key_unique (schema drift detected)';
    ELSE
        RAISE NOTICE 'UNIQUE (job_id, node_key) already present — migration is a no-op';
    END IF;
END $$;
