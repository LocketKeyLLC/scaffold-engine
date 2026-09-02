-- §17.900 — a role override must carry its PROVIDER, not just a model tag.
-- Before this, `model_overrides` stored (role, model) only, so provider was
-- env-only + restart-only and `PUT /models/roles/{role}` validated every tag
-- against the pulled Ollama list — you could not point a role at gpt-5 or
-- claude-opus-5 even though both providers already worked. NULL means "use the
-- role's configured/default provider", so every existing row keeps its exact
-- current behavior. Single statement (asyncpg runner).
ALTER TABLE model_overrides ADD COLUMN IF NOT EXISTS provider TEXT
