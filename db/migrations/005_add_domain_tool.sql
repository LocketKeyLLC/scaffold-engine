-- Migration 005: Add domain and tool columns to dag_nodes
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'dag_nodes' AND column_name = 'domain'
    ) THEN
        ALTER TABLE dag_nodes ADD COLUMN domain VARCHAR(10);
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'dag_nodes' AND column_name = 'tool'
    ) THEN
        ALTER TABLE dag_nodes ADD COLUMN tool VARCHAR(50) DEFAULT 'LLM';
    END IF;
END;
$$;
