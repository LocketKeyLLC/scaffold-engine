"""§17.772 — unit tests for MCP client result-shaping helpers.

Hermetic: exercises the pure functions that flatten a CallToolResult and read
the error flag across the camelCase/snake_case SDK spellings. The live
transport paths (stdio + streamable_http) are covered by manual/integration
smoke against a real server, not here.
"""
from __future__ import annotations

from types import SimpleNamespace as NS

import pytest

from app.modules.mcp_client import (
    McpToolResult,
    _is_error,
    _result_to_text,
    _tool_to_dict,
)

pytestmark = pytest.mark.smoke


class TestIsError:
    def test_snake_case_true(self):
        assert _is_error(NS(is_error=True)) is True

    def test_snake_case_false(self):
        assert _is_error(NS(is_error=False)) is False

    def test_camel_case_fallback(self):
        # snake missing -> fall back to camelCase (older SDK)
        assert _is_error(NS(isError=True)) is True

    def test_default_false_when_absent(self):
        assert _is_error(NS()) is False


class TestResultToText:
    def test_prefers_structured_content(self):
        r = NS(structured_content={"result": 5}, content=[NS(text="ignored")])
        out = _result_to_text(r)
        assert '"result": 5' in out

    def test_joins_text_blocks(self):
        r = NS(structured_content=None, content=[NS(text="line1"), NS(text="line2")])
        assert _result_to_text(r) == "line1\nline2"

    def test_empty_content(self):
        r = NS(structured_content=None, content=[])
        assert _result_to_text(r) == ""

    def test_non_text_block_labeled(self):
        r = NS(structured_content=None, content=[NS(text=None, type="image", data=b"x")])
        assert "[image]" in _result_to_text(r)


class TestToolToDict:
    def test_camelcase_input_schema(self):
        t = NS(name="echo", description="d", inputSchema={"type": "object"})
        assert _tool_to_dict(t) == {
            "name": "echo",
            "description": "d",
            "input_schema": {"type": "object"},
        }

    def test_missing_desc_and_schema_defaults(self):
        d = _tool_to_dict(NS(name="x"))
        assert d["description"] == ""
        assert d["input_schema"] == {}


def test_tool_result_dataclass_defaults():
    r = McpToolResult(text="hi")
    assert r.is_error is False
    assert r.structured is None
    assert r.raw_content == []
