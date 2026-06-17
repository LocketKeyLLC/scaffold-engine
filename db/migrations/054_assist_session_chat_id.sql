-- Migration 054: durable chat_id on assist_sessions (§17.538)
--
-- §17.537 made plain chat in an ACTIVE assist session route to step guidance
-- instead of triage, gated on the per-chat session map. But that map lives
-- ONLY in Redis (`assist:chatmap:v1:<chat_id>`), and this host runs Redis with
-- `maxmemory-policy allkeys-lru` at a 2 GB cap shared with the embedding cache.
-- The chatmap key is tiny and rarely read → prime LRU-eviction bait under
-- memory pressure. When it's evicted, an active session is silently orphaned
-- from its chat and plain text drops back to triage (the user-reported
-- recurrence: the §17.537 fix was live + the session still active, but the
-- chatmap key had been evicted, so the gate saw nothing).
--
-- Fix: persist the chat→session link durably on the session row. The chatmap
-- PUT now also writes `assist_sessions.chat_id`; the chatmap GET falls back to
-- Postgres (active session for this chat) on a Redis miss and re-seeds Redis
-- (self-heal). Redis stays the fast path; Postgres is the durable backstop.
--
-- Nullable, backfilled lazily on the next chatmap PUT per chat (no data
-- migration needed). The recovery query filters `chat_id = :cid AND status =
-- 'active'`; assist_sessions is a small table so no index is required.
--
-- ONE statement on purpose: the runner (app/migrations.py) executes each file
-- through asyncpg's prepared-statement path, which rejects multiple
-- semicolon-separated commands. `ADD COLUMN IF NOT EXISTS` is idempotent, so
-- re-applying is a no-op. The 023 update_updated_at trigger still fires on
-- UPDATEs that write this column.

ALTER TABLE assist_sessions
    ADD COLUMN IF NOT EXISTS chat_id TEXT;
