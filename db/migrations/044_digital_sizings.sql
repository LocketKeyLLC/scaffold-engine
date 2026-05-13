-- 044_digital_sizings.sql
-- §17.152 — Digital sizing audit table. Mirror of §17.147's
-- device_sizings but for ``design.kind = 'digital_logic'`` flows.
-- Persists one row per attempt to size a digital design (Verilator-
-- in-the-loop, not ngspice).
--
-- Differences from device_sizings:
--   * final_sv_source vs final_netlist — semantic naming for the
--     payload (SystemVerilog source, not SPICE).
--   * top_module — Verilator requires the top-module name explicitly.
--     Defaults to 'tb' by convention (testbench module name).
--
-- Same audit-the-attempt rule from §17.147: a row IS the attempt,
-- converged BOOL is the outcome. sim_run_ids[] points to sim_runs
-- rows with tool='verilator'.
--
-- CASCADE from spec_id AND topology_selection_id, same as device_sizings.
-- DO-block wrapped per the asyncpg multi-statement rule.

DO $$
BEGIN
    CREATE TABLE IF NOT EXISTS digital_sizings (
        id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        spec_id                 UUID NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
        topology_selection_id   UUID NOT NULL REFERENCES topology_selections(id) ON DELETE CASCADE,
        candidate_idx           INTEGER NOT NULL,
        final_params            JSONB NOT NULL DEFAULT '{}'::jsonb,
        final_sv_source         TEXT NOT NULL DEFAULT '',
        top_module              TEXT NOT NULL DEFAULT 'tb',
        sim_run_ids             UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
        converged               BOOLEAN NOT NULL,
        iterations              INTEGER NOT NULL,
        model_used              TEXT NOT NULL,
        measurements_final      JSONB NOT NULL DEFAULT '{}'::jsonb,
        errors                  TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
        created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_digital_sizings_spec_id
        ON digital_sizings(spec_id);

    CREATE INDEX IF NOT EXISTS idx_digital_sizings_topology_selection_id
        ON digital_sizings(topology_selection_id);

    CREATE INDEX IF NOT EXISTS idx_digital_sizings_created_at
        ON digital_sizings(created_at DESC);

    CREATE INDEX IF NOT EXISTS idx_digital_sizings_converged
        ON digital_sizings(spec_id, created_at DESC)
        WHERE converged = TRUE;
END $$;
