-- Migration 003: Add compiled_output column to jobs table
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'jobs' AND column_name = 'compiled_output'
    ) THEN
        ALTER TABLE jobs ADD COLUMN compiled_output TEXT;
    END IF;
END;
$$;
