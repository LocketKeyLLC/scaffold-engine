-- 046_formal_verifications.sql
-- §17.414 — Formal-verification audit table. Records one row per attempt
-- to formally verify a converged digital design (symbiyosys-in-the-loop,
-- closed-loop repair). Sits between the §17.152 digital-sizing stage and
-- the §17.148 report stage in the engineering-design pipeline.
--
-- Relationship to the existing sim tables:
--   * Mirrors §17.152's digital_sizings audit shape (audit-the-attempt:
--     a row IS the attempt, converged BOOL is the outcome).
--   * digital_sizing_id points at the DUT this run verified — the formal
--     stage reuses the converged DUT from digital_sizings as its starting
--     point (the "hybrid" property-source design: reuse DUT + LLM-authored
--     SVA harness derived from the spec's constraints).
--   * sim_run_ids[] points to sim_runs rows with tool='symbiyosys'; the
--     verdict / depth_reached attestations live there per attempt.
--
-- converged = (final verdict == 'PASS'). dut_source / properties_source
-- record the final iteration's formal-clean DUT and its SVA property set
-- so the report can render them and an auditor can reproduce the run.
--
-- CASCADE from spec_id, topology_selection_id, AND digital_sizing_id.
-- DO-block wrapped per the asyncpg multi-statement rule.

DO $$
BEGIN
    CREATE TABLE IF NOT EXISTS formal_verifications (
        id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        spec_id                 UUID NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
        topology_selection_id   UUID NOT NULL REFERENCES topology_selections(id) ON DELETE CASCADE,
        digital_sizing_id       UUID NOT NULL REFERENCES digital_sizings(id) ON DELETE CASCADE,
        candidate_idx           INTEGER NOT NULL,
        dut_source              TEXT NOT NULL DEFAULT '',
        properties_source       TEXT NOT NULL DEFAULT '',
        top_module              TEXT NOT NULL DEFAULT 'tb',
        mode                    TEXT NOT NULL DEFAULT 'bmc',
        depth                   INTEGER NOT NULL DEFAULT 20,
        engine                  TEXT NOT NULL DEFAULT 'smtbmc z3',
        verdict                 TEXT,
        depth_reached           INTEGER,
        converged               BOOLEAN NOT NULL,
        iterations              INTEGER NOT NULL,
        model_used              TEXT NOT NULL,
        sim_run_ids             UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
        errors                  TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
        created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_formal_verifications_spec_id
        ON formal_verifications(spec_id);

    CREATE INDEX IF NOT EXISTS idx_formal_verifications_topology_selection_id
        ON formal_verifications(topology_selection_id);

    CREATE INDEX IF NOT EXISTS idx_formal_verifications_digital_sizing_id
        ON formal_verifications(digital_sizing_id);

    CREATE INDEX IF NOT EXISTS idx_formal_verifications_created_at
        ON formal_verifications(created_at DESC);

    CREATE INDEX IF NOT EXISTS idx_formal_verifications_converged
        ON formal_verifications(digital_sizing_id, created_at DESC)
        WHERE converged = TRUE;
END $$;
