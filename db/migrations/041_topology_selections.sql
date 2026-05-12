-- 041_topology_selections.sql
-- §17.146 — Topology-selection audit table. The first downstream
-- reasoning stage in the engineering-design pipeline (after the
-- §17.143 schema, §17.144 extractor, and §17.145 /confirm gate).
-- Persists one row per ``POST /specs/{id}/topology-select`` call.
--
-- The schema is intentionally narrow — it stores what the §17.146
-- citation-enforcement contract requires and nothing more:
--   * candidates: the LLM's proposed topologies (list of dicts with
--                 name / description / rationale / citations[]).
--   * rag_chunk_ids: the entry_ids of every chunk the LLM was shown.
--                    The verifiability invariant is that every
--                    citation in `candidates` must appear in this
--                    array, so storing it explicitly makes the audit
--                    self-contained (no need to replay the RAG query
--                    to know what the LLM saw).
--   * rag_query / rag_domain: enough metadata to reproduce the
--                             retrieval, but we don't store the
--                             full chunk content here — Milvus is
--                             the authoritative store for that.
--
-- ON DELETE CASCADE from specs: a deleted spec invalidates its
-- selections (the citations may point at chunks the corpus has
-- since dropped, and the selections themselves are derived data).
--
-- Wrapped in a DO block per the asyncpg multi-statement rule
-- (§17.140 / 032_system_alerts.sql).

DO $$
BEGIN
    CREATE TABLE IF NOT EXISTS topology_selections (
        id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        spec_id         UUID NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
        candidates      JSONB NOT NULL,
        rag_chunk_ids   TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
        rag_query       TEXT NOT NULL,
        rag_domain      TEXT,
        model_used      TEXT NOT NULL,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_topology_selections_spec_id
        ON topology_selections(spec_id);

    CREATE INDEX IF NOT EXISTS idx_topology_selections_created_at
        ON topology_selections(created_at DESC);
END $$;
