-- Sprint X.26 — system_alerts table.
-- Backs the file+DB sink used by `app/observability/alerts.py`. Fires from:
--   * scripts/quarterly_calibration_pr.sh  (calibration failure / no-fire watchdog)
--   * app/observability/thresholds.py      (X.20 rollup threshold breach)
--   * app/observability/alerts.py CLI      (operator-emitted)
--
-- dedup_key + cooldown is enforced application-side (alerts.py) because
-- the cooldown is configurable per deployment; the partial index here
-- just keeps that lookup cheap.
--
-- Single DO block per X.5's lesson: the migration runner uses asyncpg's
-- prepared-statement protocol, which rejects multi-statement bodies.

DO $$
BEGIN
    CREATE TABLE IF NOT EXISTS system_alerts (
        id          UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
        kind        TEXT        NOT NULL,
        severity    TEXT        NOT NULL CHECK (severity IN ('info','warning','critical')),
        message     TEXT        NOT NULL,
        payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,
        dedup_key   TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
    );

    CREATE INDEX IF NOT EXISTS idx_system_alerts_created_at
        ON system_alerts (created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_system_alerts_kind_created
        ON system_alerts (kind, created_at DESC);
    CREATE INDEX IF NOT EXISTS idx_system_alerts_dedup_key_created
        ON system_alerts (dedup_key, created_at DESC) WHERE dedup_key IS NOT NULL;
END $$;
