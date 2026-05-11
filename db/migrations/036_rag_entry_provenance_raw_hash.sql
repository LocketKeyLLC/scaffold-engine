-- 036_rag_entry_provenance_raw_hash.sql
-- §17.126 — Record a hash of raw upstream bytes per provenance row so
-- /research/verify?recheck=true&compare_hash=true can detect content
-- drift (upstream source mutated since ingest).
--
-- Nullable: pre-§17.126 rows and rows from producers that haven't
-- wired raw_upstream_hash yet carry NULL. Verify endpoint reports
-- ``content_state=unverifiable`` for NULL entries.
--
-- The hash domain is producer-chosen — for arXiv it's the Atom XML
-- body bytes; for SHA-pinned GitHub blobs it would be the blob bytes;
-- for JSON-API forum sources it would be a canonical-JSON serialization
-- of the relevant fields. Each producer picks what's stable.
--
-- Idempotent — IF NOT EXISTS guard.

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name   = 'rag_entry_provenance'
           AND column_name  = 'raw_upstream_hash'
    ) THEN
        EXECUTE 'ALTER TABLE rag_entry_provenance ADD COLUMN raw_upstream_hash TEXT';
    END IF;
END $$;
