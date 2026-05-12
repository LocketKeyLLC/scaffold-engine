-- 039_sim_runs_verdict.sql
-- §17.142 — Add `verdict` column to sim_runs for tools whose primary
-- output is a categorical pass/fail/unknown verdict rather than a set
-- of numeric KPIs. SymbiYosys is the first such tool — its formal
-- verification engines emit PASS / FAIL / UNKNOWN / ERROR. ngspice and
-- verilator can also populate it (e.g. "OK" / "FAIL" based on a
-- testbench-emitted summary line) but their primary contract remains
-- the `measurements` JSONB payload.
--
-- Nullable + no default — a NULL verdict means "this tool doesn't
-- emit a categorical verdict" (correct for ngspice/verilator today;
-- only symbiyosys will write here).
--
-- Wrapped in a DO block per asyncpg multi-statement rule (see §17.140
-- / 032_system_alerts.sql).

DO $$
BEGIN
    ALTER TABLE sim_runs
        ADD COLUMN IF NOT EXISTS verdict TEXT;

    CREATE INDEX IF NOT EXISTS idx_sim_runs_verdict
        ON sim_runs(verdict)
        WHERE verdict IS NOT NULL;
END $$;
