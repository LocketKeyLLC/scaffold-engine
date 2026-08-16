-- 066_api_keys.sql
-- §17.807 — install-time multi-user option: scoped API keys.
--
-- When MULTI_USER_ENABLED=true, the orchestrator accepts additional named
-- API keys (beyond the master SCAFFOLD_API_KEY, which stays valid as the
-- admin/bootstrap key). Each row is one revocable key: only the SHA-256 hex
-- digest of the raw key is stored — the raw value is shown once at mint time
-- (`make key-add`) and never persisted. Auth (app/auth.py) hashes the
-- presented X-API-Key and looks it up here WHERE revoked_at IS NULL; any live
-- match passes with equal access (§17.807 "named keys, equal access" — no
-- per-key permission tiers, no per-user job isolation).
--
-- The table is created unconditionally (cheap, empty until a key is minted);
-- the MULTI_USER_ENABLED valve is what actually gates whether these rows are
-- consulted at request time, so single-user installs are unaffected.
--
-- Single statement: table + indexes are created inside one `DO $$ … $$` block
-- (per §17.140 — the asyncpg prepared-statement runner path chokes silently on
-- multi-`;` files), each guarded IF NOT EXISTS so re-apply is a no-op. Mirrors
-- migration 065's shape.
DO $$
BEGIN
    -- ── 1. api_keys table ─────────────────────────────────────────────────
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.tables
         WHERE table_schema = current_schema()
           AND table_name   = 'api_keys'
    ) THEN
        EXECUTE 'CREATE TABLE api_keys (
            id          BIGSERIAL PRIMARY KEY,
            key_hash    TEXT        NOT NULL UNIQUE,
            label       TEXT        NOT NULL,
            owner       TEXT,
            created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            revoked_at  TIMESTAMPTZ
        )';
    END IF;

    -- ── 2. live-key lookup index (partial: the auth hot path) ─────────────
    -- Auth resolves every non-admin request via
    --   SELECT 1 FROM api_keys WHERE key_hash = $1 AND revoked_at IS NULL
    -- so index exactly that predicate. The UNIQUE(key_hash) constraint above
    -- already indexes the column, but a partial index over live rows keeps the
    -- probe tight as revoked keys accumulate.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = current_schema()
           AND indexname  = 'idx_api_keys_live_hash'
    ) THEN
        EXECUTE 'CREATE INDEX idx_api_keys_live_hash
                   ON api_keys (key_hash) WHERE revoked_at IS NULL';
    END IF;
END $$;
