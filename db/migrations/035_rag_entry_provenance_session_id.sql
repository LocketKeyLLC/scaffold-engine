-- 035_rag_entry_provenance_session_id.sql
-- §17.114 — Link each provenance row to the research session that ingested it.
--
-- Enables /research/verify/{session_id}: re-fetch every entry's source_ref
-- and confirm upstream content still matches what was ingested.
--
-- Nullable column — entries written before §17.114 (or via paths that
-- don't pass session_id, e.g., /rag/ingest direct API) carry NULL. The
-- verify endpoint scopes by exact session_id match, so NULL rows are
-- correctly invisible to it.
--
-- Idempotent — IF NOT EXISTS guards on column + index.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name   = 'rag_entry_provenance'
           AND column_name  = 'session_id'
    ) THEN
        EXECUTE 'ALTER TABLE rag_entry_provenance ADD COLUMN session_id UUID';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = current_schema()
           AND indexname  = 'idx_rag_entry_provenance_session_id'
    ) THEN
        EXECUTE 'CREATE INDEX idx_rag_entry_provenance_session_id
                 ON rag_entry_provenance(session_id)
                 WHERE session_id IS NOT NULL';
    END IF;
END $$;
