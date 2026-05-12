-- 040_specs.sql
-- §17.143 — Engineering-design spec table. Persists the validated
-- JSON document produced (eventually) by the NL→spec extractor and
-- referenced by the design pipeline's verification stage. The schema
-- itself lives in app/sim/spec_schema.json; this table just stores
-- already-validated instances.
--
-- ``confirmed_by`` / ``confirmed_at`` are populated when the human
-- operator hits /confirm — those columns stay NULL on the initial
-- INSERT and are written by the /confirm gate handler in a future
-- commit. A spec without a confirmation row CANNOT advance the job
-- past spec_capture (enforced application-side).
--
-- ``spec_sha256`` matches what ``app.sim.spec.spec_sha256`` computes,
-- so a deterministic re-extraction of the same NL prompt yields the
-- same hash — enabling future dedup / cache without re-validating.
--
-- ``job_id`` is nullable: a spec can be drafted (e.g. during a
-- ``/spec dry-run``) before it's bound to a job. The application
-- guarantees that a job's running stages reference a job-bound spec.
--
-- Wrapped in a DO block per the asyncpg multi-statement rule
-- (§17.140 / 032_system_alerts.sql).

DO $$
BEGIN
    CREATE TABLE IF NOT EXISTS specs (
        id              UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
        job_id          UUID REFERENCES jobs(id) ON DELETE CASCADE,
        schema_version  TEXT NOT NULL,
        spec_json       JSONB NOT NULL,
        spec_sha256     TEXT NOT NULL,
        confirmed_by    TEXT,
        confirmed_at    TIMESTAMPTZ,
        created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_specs_job_id
        ON specs(job_id)
        WHERE job_id IS NOT NULL;

    CREATE INDEX IF NOT EXISTS idx_specs_spec_sha256
        ON specs(spec_sha256);

    CREATE INDEX IF NOT EXISTS idx_specs_created_at
        ON specs(created_at DESC);

    -- Partial index for fast "give me confirmed specs" lookups —
    -- the /confirm gate handler will use this to short-circuit.
    CREATE INDEX IF NOT EXISTS idx_specs_confirmed_at
        ON specs(confirmed_at DESC)
        WHERE confirmed_at IS NOT NULL;
END $$;
