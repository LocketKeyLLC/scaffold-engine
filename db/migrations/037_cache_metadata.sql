-- 037_cache_metadata.sql
-- §17.135 — Generic key/value table for cross-restart cache state.
--
-- First user: ``active_embedder_id``. On lifespan startup, the
-- check_embedder_drift helper compares the configured
-- ``MODEL_EMBEDDER_PIPELINE`` to the value stored here. A mismatch is
-- silent retrieval-quality death — the Milvus collection holds vectors
-- from model A, but new queries get embedded by model B, and the
-- vector space is no longer coherent. The drift check emits a critical
-- system_alerts row pointing at scripts/reindex.py.
--
-- Design intentionally kept generic so future cache versions (rag
-- result cache prefix bumps, fetch cache schema versions, etc.) can
-- piggyback without another migration per concern.
--
-- Idempotent — CREATE TABLE IF NOT EXISTS.

CREATE TABLE IF NOT EXISTS cache_metadata (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
