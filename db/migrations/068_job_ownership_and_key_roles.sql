-- 068_job_ownership_and_key_roles.sql
-- §17.810 — per-user job ownership + basic RBAC (extends §17.807 multi-user).
--
-- §17.807 (mig 066) shipped "named keys, equal access": every live key in
-- api_keys authenticates identically, with no per-key role and no per-user job
-- isolation. This migration adds the two columns that turn that flat model into
-- basic RBAC + ownership:
--
--   * api_keys.role  — 'admin' | 'user' (default 'user'). The role a presented
--     key resolves to. The master SCAFFOLD_API_KEY is always admin (checked in
--     app/auth.py, not stored here), so pre-existing scoped keys defaulting to
--     'user' is the safe, least-privilege choice.
--
--   * jobs.owner     — the principal identity (a key's `owner` tag, so multiple
--     keys can share one user; see app/authz.py) that created the job. NULL for
--     legacy rows and for jobs created before this migration; those are visible
--     only to admins. Stamped on every create path going forward.
--
-- Enforcement is gated by MULTI_USER_ENABLED (app/config.py): when off, every
-- request resolves to the admin principal, so the owner predicate is a no-op and
-- single-user installs see zero behavior change.
--
-- Single statement: everything runs inside one `DO $$ … $$` block (per §17.140 —
-- the asyncpg prepared-statement runner path chokes silently on multi-`;` files),
-- each step guarded so re-apply is a no-op. Mirrors migration 066's shape.
DO $$
BEGIN
    -- ── 1. api_keys.role ──────────────────────────────────────────────────
    -- NOT NULL DEFAULT 'user': existing rows are backfilled to 'user' by the
    -- default, matching the least-privilege intent (an operator promotes a key
    -- to admin explicitly via `make key-add ... ROLE=admin`).
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name   = 'api_keys'
           AND column_name  = 'role'
    ) THEN
        EXECUTE 'ALTER TABLE api_keys
                   ADD COLUMN role TEXT NOT NULL DEFAULT ''user''';
    END IF;

    -- Role domain guard. ADD CONSTRAINT has no IF NOT EXISTS, so gate on the
    -- catalog. Named so the guard and the DDL reference the same constraint.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
         WHERE conname = 'api_keys_role_check'
    ) THEN
        EXECUTE 'ALTER TABLE api_keys
                   ADD CONSTRAINT api_keys_role_check
                   CHECK (role IN (''admin'', ''user''))';
    END IF;

    -- ── 2. jobs.owner ─────────────────────────────────────────────────────
    -- Nullable on purpose: pre-existing jobs have no known owner and must not
    -- be forced to a synthetic one (that would silently reassign them). NULL =
    -- "unowned / legacy", visible only to admins (app/authz.py owner_filter).
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name   = 'jobs'
           AND column_name  = 'owner'
    ) THEN
        EXECUTE 'ALTER TABLE jobs ADD COLUMN owner TEXT';
    END IF;

    -- Ownership-scoped listing hits `WHERE owner = $1` for every non-admin
    -- request; index exactly that. Partial over non-NULL owners keeps the index
    -- off the legacy/admin rows that never match a user predicate.
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = current_schema()
           AND indexname  = 'idx_jobs_owner'
    ) THEN
        EXECUTE 'CREATE INDEX idx_jobs_owner
                   ON jobs (owner) WHERE owner IS NOT NULL';
    END IF;

    -- ── 3. research_sessions.owner ────────────────────────────────────────
    -- Research sessions are STANDALONE (no job_id FK), so ownership can't be
    -- derived from a parent job — it lives on the row itself. Same nullable /
    -- legacy-is-admin-only semantics as jobs.owner.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name   = 'research_sessions'
           AND column_name  = 'owner'
    ) THEN
        EXECUTE 'ALTER TABLE research_sessions ADD COLUMN owner TEXT';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = current_schema()
           AND indexname  = 'idx_research_sessions_owner'
    ) THEN
        EXECUTE 'CREATE INDEX idx_research_sessions_owner
                   ON research_sessions (owner) WHERE owner IS NOT NULL';
    END IF;

    -- ── 4. scheduled_jobs.owner ───────────────────────────────────────────
    -- Also standalone (SERIAL PK, spawns research_sessions rather than jobs).
    -- The owner stamped here is propagated to each spawned research session so a
    -- schedule's runs stay attributed to whoever created the schedule.
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND table_name   = 'scheduled_jobs'
           AND column_name  = 'owner'
    ) THEN
        EXECUTE 'ALTER TABLE scheduled_jobs ADD COLUMN owner TEXT';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM pg_indexes
         WHERE schemaname = current_schema()
           AND indexname  = 'idx_scheduled_jobs_owner'
    ) THEN
        EXECUTE 'CREATE INDEX idx_scheduled_jobs_owner
                   ON scheduled_jobs (owner) WHERE owner IS NOT NULL';
    END IF;
END $$;
