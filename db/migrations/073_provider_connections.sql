-- §17.900 — per-provider connection settings, editable at runtime from the UI.
-- Before this, every provider credential (OPENAI_API_KEY, ANTHROPIC_API_KEY,
-- base URLs) was env-only: connecting ChatGPT or Claude meant editing .env and
-- restarting the container. `api_key_enc` holds a Fernet ciphertext (never
-- plaintext, never echoed back by the API — see app/utils/secrets.py); a NULL
-- means "fall back to the env value", so an existing .env-configured install
-- keeps working with no row at all. Single statement (asyncpg runner).
CREATE TABLE IF NOT EXISTS provider_connections (
    provider    TEXT PRIMARY KEY,
    api_key_enc TEXT,
    base_url    TEXT,
    enabled     BOOLEAN NOT NULL DEFAULT TRUE,
    label       TEXT,
    last_ok_at  TIMESTAMPTZ,
    last_error  TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
