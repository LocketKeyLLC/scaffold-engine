"""§17.772 — unit tests for the MCP server registry.

Hermetic: config-seed parsing, spec validation, secret redaction, JSONB
coercion, and the config-under-DB merge (DB mocked). No live MCP server.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.modules import mcp_registry
from app.modules.mcp_registry import (
    McpServerSpec,
    _coerce_json,
    list_servers,
    parse_config_seed,
)

pytestmark = pytest.mark.smoke


def _row(name, **over):
    base = {
        "name": name,
        "transport": "stdio",
        "endpoint": None,
        "command": "python",
        "args": "[]",
        "env": None,
        "headers": None,
        "enabled": True,
        "description": None,
    }
    base.update(over)
    return base


def _mock_db(rows):
    db = MagicMock()
    proxy = MagicMock()
    proxy.mappings.return_value.all.return_value = rows
    db.execute = AsyncMock(return_value=proxy)
    return db


class TestSpecValidation:
    def test_stdio_requires_command(self):
        with pytest.raises(ValueError):
            McpServerSpec(name="x", transport="stdio").validate()

    def test_http_requires_endpoint(self):
        with pytest.raises(ValueError):
            McpServerSpec(name="x", transport="streamable_http").validate()

    def test_unknown_transport(self):
        with pytest.raises(ValueError):
            McpServerSpec(name="x", transport="carrier-pigeon").validate()

    def test_valid_stdio(self):
        McpServerSpec(name="x", transport="stdio", command="python").validate()

    def test_valid_http(self):
        McpServerSpec(name="x", transport="streamable_http", endpoint="http://h/mcp").validate()


class TestRedaction:
    def test_public_dict_redacts_values_keeps_keys(self):
        spec = McpServerSpec(
            name="s",
            transport="streamable_http",
            endpoint="http://h/mcp",
            headers={"Authorization": "Bearer topsecret"},
            env={"TOKEN": "abc123"},
        )
        d = spec.public_dict()
        blob = json.dumps(d)
        assert d["header_keys"] == ["Authorization"]
        assert d["env_keys"] == ["TOKEN"]
        assert "topsecret" not in blob
        assert "abc123" not in blob


class TestConfigSeed:
    def test_empty(self, monkeypatch):
        monkeypatch.setattr(mcp_registry.settings, "mcp_servers_config", "[]")
        assert parse_config_seed() == {}

    def test_malformed_json_returns_empty(self, monkeypatch):
        monkeypatch.setattr(mcp_registry.settings, "mcp_servers_config", "{not json")
        assert parse_config_seed() == {}

    def test_not_a_list_returns_empty(self, monkeypatch):
        monkeypatch.setattr(mcp_registry.settings, "mcp_servers_config", '{"name":"x"}')
        assert parse_config_seed() == {}

    def test_skips_invalid_keeps_valid(self, monkeypatch):
        cfg = json.dumps(
            [
                {"name": "good", "transport": "stdio", "command": "python"},
                {"name": "bad", "transport": "stdio"},  # missing command
                {"transport": "stdio", "command": "x"},  # missing name
            ]
        )
        monkeypatch.setattr(mcp_registry.settings, "mcp_servers_config", cfg)
        seed = parse_config_seed()
        assert set(seed) == {"good"}
        assert seed["good"].source == "config"


class TestCoerceJson:
    def test_str_decoded(self):
        assert _coerce_json('["a", "b"]', []) == ["a", "b"]

    def test_dict_passthrough(self):
        assert _coerce_json({"a": 1}, {}) == {"a": 1}

    def test_none_returns_default(self):
        assert _coerce_json(None, []) == []

    def test_bad_str_returns_default(self):
        assert _coerce_json("{bad", {"d": 1}) == {"d": 1}


class TestMerge:
    async def test_db_overrides_config_by_name(self, monkeypatch):
        cfg = json.dumps(
            [
                {"name": "dup", "transport": "stdio", "command": "config-cmd"},
                {"name": "only-cfg", "transport": "stdio", "command": "c"},
            ]
        )
        monkeypatch.setattr(mcp_registry.settings, "mcp_servers_config", cfg)
        db = _mock_db([_row("dup", command="db-cmd"), _row("only-db")])
        specs = await list_servers(db)
        by_name = {s.name: s for s in specs}
        assert set(by_name) == {"dup", "only-cfg", "only-db"}
        assert by_name["dup"].command == "db-cmd"  # DB wins
        assert by_name["dup"].source == "db"
        assert by_name["only-cfg"].source == "config"

    async def test_disabled_filtered_by_default(self, monkeypatch):
        monkeypatch.setattr(mcp_registry.settings, "mcp_servers_config", "[]")
        db = _mock_db([_row("off", enabled=False), _row("on")])
        assert [s.name for s in await list_servers(db)] == ["on"]
        assert len(await list_servers(db, include_disabled=True)) == 2

    async def test_args_json_string_coerced(self, monkeypatch):
        monkeypatch.setattr(mcp_registry.settings, "mcp_servers_config", "[]")
        db = _mock_db([_row("s", args='["-y", "pkg"]')])
        specs = await list_servers(db)
        assert specs[0].args == ["-y", "pkg"]
