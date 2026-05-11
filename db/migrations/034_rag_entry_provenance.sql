-- 034_rag_entry_provenance.sql
-- §17.104 — Provenance sidecar for RAG entries (deep-search phase 1).
--
-- Stores the upstream ref + fetch time + quality signal for each Milvus
-- toon_v2 row. Keyed by entry_id; no FK because the authoritative row
-- lives in Milvus, not Postgres. Orphans (entry purged by staleness
-- sweep) are harmless garbage.
--
-- Idempotent — IF NOT EXISTS guards on table + index.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = current_schema()
           AND table_name   = 'rag_entry_provenance'
    ) THEN
        EXECUTE '
            CREATE TABLE rag_entry_provenance (
                entry_id        TEXT PRIMARY KEY,
                source_ref      TEXT NOT NULL DEFAULT '''',
                fetched_at      BIGINT NOT NULL,
                quality_signal  JSONB NOT NULL DEFAULT ''{}''::jsonb,
                created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        ';
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = current_schema()
           AND indexname  = 'idx_rag_entry_provenance_fetched_at'
    ) THEN
        EXECUTE 'CREATE INDEX idx_rag_entry_provenance_fetched_at
                 ON rag_entry_provenance (fetched_at)';
    END IF;
END $$;
