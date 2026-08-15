-- 060_mcp_servers_and_node_tool_config.sql
-- §17.772 — Model Context Protocol integration (consumer side).
--
-- Adds:
--   1. mcp_servers — DB registry of external MCP servers the engine may call
--      as DAG nodes (tool='MCP'). Merged OVER the settings.mcp_servers_config
--      seed by app/modules/mcp_registry.py (a DB row overrides a config entry
--      with the same name). `name` is the natural key DAG nodes reference.
--   2. dag_nodes.tool_config JSONB — a generic per-node config blob. For an
--      MCP node it carries {"server": "...", "tool": "...", "args": {...}}.
--      Left generic (not MCP-specific) so the dormant Shell seam can reuse it.
--
-- ONE statement (single DO block) per the asyncpg "no multiple commands in a
-- prepared statement" rule (§17.140). All additive / IF NOT EXISTS — safe to
-- re-run; no backfill (tool_config defaults NULL for existing rows).

DO $mig$
BEGIN
    ALTER TABLE dag_nodes
        ADD COLUMN IF NOT EXISTS tool_config JSONB;

    CREATE TABLE IF NOT EXISTS mcp_servers (
        name        TEXT PRIMARY KEY,
        transport   TEXT NOT NULL
                        CHECK (transport IN ('streamable_http', 'stdio')),
        endpoint    TEXT,                 -- streamable_http URL (e.g. http://host:9000/mcp)
        command     TEXT,                 -- stdio launcher (e.g. npx, python)
        args        JSONB NOT NULL DEFAULT '[]'::jsonb,   -- stdio argv tail
        env         JSONB,                -- stdio subprocess env overrides
        headers     JSONB,                -- streamable_http request headers (auth)
        enabled     BOOLEAN NOT NULL DEFAULT true,
        description TEXT,
        created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
    );

    -- A stdio server needs a command; an http server needs an endpoint.
    -- Enforced softly at the app layer too (mcp_registry validation), but the
    -- CHECK stops obviously-malformed rows at write time.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'mcp_servers_transport_fields'
    ) THEN
        ALTER TABLE mcp_servers
            ADD CONSTRAINT mcp_servers_transport_fields CHECK (
                (transport = 'stdio' AND command IS NOT NULL)
                OR (transport = 'streamable_http' AND endpoint IS NOT NULL)
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_mcp_servers_updated_at'
    ) THEN
        CREATE TRIGGER trg_mcp_servers_updated_at
            BEFORE UPDATE ON mcp_servers
            FOR EACH ROW EXECUTE FUNCTION update_updated_at();
    END IF;

    COMMENT ON TABLE mcp_servers IS
        'External MCP servers callable as DAG nodes (§17.772). Merged over settings.mcp_servers_config by name.';
    COMMENT ON COLUMN dag_nodes.tool_config IS
        'Generic per-node config (§17.772). MCP nodes: {"server","tool","args"}.';
END
$mig$;
