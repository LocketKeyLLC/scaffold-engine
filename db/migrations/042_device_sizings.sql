-- 042_device_sizings.sql
-- §17.147 — Device-sizing audit table. First closed-loop stage in
-- the engineering-design pipeline: takes a confirmed spec + a chosen
-- topology candidate, runs an LLM/SPICE iteration until the
-- constraints are met or the budget is exhausted.
--
-- Persistence semantics differ from §17.146 (topology_selections):
-- we DO persist a row even when the loop didn't converge. The row
-- is an audit of the *attempt*, not just successful outcomes — an
-- operator looking at "why is this spec stuck?" needs to see what
-- was tried and what the measurements were. ``converged`` distin-
-- guishes outcome; ``ok`` at the API layer is True only when
-- ``converged = TRUE``.
--
-- ``sim_run_ids`` is a UUID[] referencing rows in ``sim_runs``
-- (one per iteration's ngspice call). The trio of oracle wrappers
-- (§17.140–142) already write to sim_runs; this column is just the
-- join from a sizing attempt to its constituent simulations.
--
-- CASCADE from spec_id AND topology_selection_id — both are the
-- attempt's inputs; if either is deleted, the attempt is stale.
--
-- Wrapped in a DO block per the asyncpg multi-statement rule.

DO $$
BEGIN
    CREATE TABLE IF NOT EXISTS device_sizings (
        id                      UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        spec_id                 UUID NOT NULL REFERENCES specs(id) ON DELETE CASCADE,
        topology_selection_id   UUID NOT NULL REFERENCES topology_selections(id) ON DELETE CASCADE,
        candidate_idx           INTEGER NOT NULL,
        final_params            JSONB NOT NULL DEFAULT '{}'::jsonb,
        final_netlist           TEXT NOT NULL DEFAULT '',
        sim_run_ids             UUID[] NOT NULL DEFAULT ARRAY[]::UUID[],
        converged               BOOLEAN NOT NULL,
        iterations              INTEGER NOT NULL,
        model_used              TEXT NOT NULL,
        measurements_final      JSONB NOT NULL DEFAULT '{}'::jsonb,
        errors                  TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
        created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_device_sizings_spec_id
        ON device_sizings(spec_id);

    CREATE INDEX IF NOT EXISTS idx_device_sizings_topology_selection_id
        ON device_sizings(topology_selection_id);

    CREATE INDEX IF NOT EXISTS idx_device_sizings_created_at
        ON device_sizings(created_at DESC);

    -- Partial index for "give me the converged sizings" lookups —
    -- the typical operator workflow filters by this.
    CREATE INDEX IF NOT EXISTS idx_device_sizings_converged
        ON device_sizings(spec_id, created_at DESC)
        WHERE converged = TRUE;
END $$;
