-- 050_model_overrides.sql
-- §17.484 — persistent per-role model overrides.
--
-- Makes the §17.483 web "set model per role" durable across restarts. A row
-- here is a deliberate operator choice to re-point a switchable role away from
-- its .env/config default; at lifespan startup these rows are loaded into the
-- live `settings` singleton (app/modules/model_overrides.load_overrides_into_
-- settings), so `get_model(role)` resolves them with no request-path DB read.
-- Clearing a row (web "reset to env") reverts the role to its config default.
--
-- Single CREATE TABLE = one statement (no DO $mig$ wrapper needed; cf. §17.140).
-- `role` is the PRIMARY KEY (one override per role, gives the lookup index).
CREATE TABLE IF NOT EXISTS model_overrides (
    role        TEXT PRIMARY KEY,
    model       TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
