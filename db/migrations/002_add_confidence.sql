-- Migration 002: Add confidence column to dag_nodes
-- Safe to re-run (idempotent via IF NOT EXISTS pattern)

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'dag_nodes' AND column_name = 'confidence'
    ) THEN
        ALTER TABLE dag_nodes ADD COLUMN confidence FLOAT DEFAULT NULL;
    END IF;
END;
$$;
