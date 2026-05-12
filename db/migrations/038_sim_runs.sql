-- 038_sim_runs.sql
-- §17.140 — Simulation-run audit table. Every invocation of a verifiable
-- engineering oracle (ngspice today; verilator/symbiyosys later) writes
-- one row here. The schema is the join point that makes every numeric
-- claim in a circuit-design report traceable: report cites sim_runs.id,
-- the row records exact tool version + netlist hash + measurements.
--
-- Minimal v1 — columns we actually populate today. Deferred until needed:
--   - waveform_path / artifact storage (no .raw dump in v1)
--   - testbench_hash (testbench == netlist in v1; will split when the
--     design pipeline starts auto-generating separate testbench files)
--
-- job_id / dag_node_id are nullable: ngspice can be called outside a job
-- context (smoke tests, ad-hoc sweeps) and we still want the audit row.
--
-- Wrapped in a single DO block — the migration runner uses asyncpg's
-- prepared-statement protocol which rejects multi-statement bodies (see
-- 032_system_alerts.sql for the same idiom).

DO $$
BEGIN
    CREATE TABLE IF NOT EXISTS sim_runs (
        id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        tool            TEXT NOT NULL,
        tool_version    TEXT NOT NULL,
        netlist_sha256  TEXT NOT NULL,
        seed            INTEGER,
        exit_code       INTEGER NOT NULL,
        stdout          TEXT NOT NULL DEFAULT '',
        stderr          TEXT NOT NULL DEFAULT '',
        measurements    JSONB NOT NULL DEFAULT '{}'::jsonb,
        duration_ms     INTEGER NOT NULL,
        timed_out       BOOLEAN NOT NULL DEFAULT FALSE,
        job_id          UUID REFERENCES jobs(id)      ON DELETE SET NULL,
        dag_node_id     UUID REFERENCES dag_nodes(id) ON DELETE SET NULL,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_sim_runs_netlist_sha256
        ON sim_runs(netlist_sha256);

    CREATE INDEX IF NOT EXISTS idx_sim_runs_job_id
        ON sim_runs(job_id)
        WHERE job_id IS NOT NULL;

    CREATE INDEX IF NOT EXISTS idx_sim_runs_created_at
        ON sim_runs(created_at DESC);
END $$;
