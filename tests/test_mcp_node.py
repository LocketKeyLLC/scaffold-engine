"""§17.772 — unit tests for MCP DAG-node arg rendering & config parsing.

Hermetic: the placeholder substitution and tool_config normalization are pure
functions. Live execution (registry resolve → client call → DB persist) is
covered by integration smoke against a real echo server.
"""
from __future__ import annotations

import pytest

from app.modules.mcp_node import (
    _lookup,
    _upstream_text,
    parse_tool_config,
    render_args,
)

pytestmark = pytest.mark.smoke


class TestParseToolConfig:
    def test_dict_passthrough(self):
        assert parse_tool_config({"server": "s"}) == {"server": "s"}

    def test_json_string(self):
        assert parse_tool_config('{"tool": "t"}') == {"tool": "t"}

    def test_bad_json_empty(self):
        assert parse_tool_config("{not json") == {}

    def test_non_object_json_empty(self):
        assert parse_tool_config("[1, 2]") == {}

    def test_none_empty(self):
        assert parse_tool_config(None) == {}


class TestUpstreamText:
    def test_tuple_takes_first(self):
        assert _upstream_text(("the output", 0.9)) == "the output"

    def test_bare_str(self):
        assert _upstream_text("plain") == "plain"

    def test_dict_output_text(self):
        assert _upstream_text({"output_text": "x"}) == "x"

    def test_none_empty(self):
        assert _upstream_text(None) == ""


class TestLookup:
    def test_upstream_hit(self):
        up = {"T1": ("hello", 1.0)}
        assert _lookup("upstream", "T1", up, {}) == "hello"

    def test_upstream_miss_returns_none(self):
        assert _lookup("upstream", "T9", {}, {}) is None

    def test_brief_dotted_path(self):
        brief = {"meta": {"domain": "rag"}}
        assert _lookup("brief", "meta.domain", {}, brief) == "rag"

    def test_brief_non_str_json_encoded(self):
        brief = {"goals": ["a", "b"]}
        assert _lookup("brief", "goals", {}, brief) == '["a", "b"]'

    def test_brief_miss_none(self):
        assert _lookup("brief", "nope", {}, {"x": 1}) is None


class TestRenderArgs:
    def test_substitutes_upstream(self):
        up = {"T1": ("result-text", 0.8)}
        out = render_args({"query": "${upstream.T1}"}, up, {})
        assert out == {"query": "result-text"}

    def test_substitutes_within_string(self):
        up = {"T1": ("world", None)}
        out = render_args({"msg": "hello ${upstream.T1}!"}, up, {})
        assert out == {"msg": "hello world!"}

    def test_unknown_placeholder_left_verbatim(self):
        out = render_args({"q": "${upstream.MISSING}"}, {}, {})
        assert out == {"q": "${upstream.MISSING}"}

    def test_nested_and_lists(self):
        up = {"T1": ("v", None)}
        out = render_args(
            {"outer": {"inner": ["${upstream.T1}", "static"]}}, up, {}
        )
        assert out == {"outer": {"inner": ["v", "static"]}}

    def test_non_string_leaves_untouched(self):
        out = render_args({"n": 42, "b": True, "x": None}, {}, {})
        assert out == {"n": 42, "b": True, "x": None}

    def test_brief_placeholder(self):
        out = render_args({"d": "${brief.domain}"}, {}, {"domain": "code"})
        assert out == {"d": "code"}
