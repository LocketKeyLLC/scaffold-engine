-- Migration 007: Add ideation workflow columns to jobs
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS research_data JSONB;
ALTER TABLE jobs ADD COLUMN IF NOT EXISTS workflow_summary TEXT;
