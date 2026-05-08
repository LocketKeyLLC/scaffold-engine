"""Sprint X.13 — read_tool_args canonical helper.

Direct unit tests on app.utils.tool_call_args.read_tool_args. The
helper used to live as 4 byte-equal copies (`_tool_args`) in
research_agent / prompt_optimizer / idea_refinement / gt_extractor;
X.13 consolidated it into a shared utility.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.utils.tool_call_args import read_tool_args


@pytest.mark.smoke
class TestReadToolArgs:
    """The contract: returns the first tool call's arguments dict, or
    None on every failure path. Callers fail-closed on None."""

    def test_returns_args_when_success_and_call_present(self):
        call = SimpleNamespace(arguments={"key": "value", "n": 42})
        resp = SimpleNamespace(success=True, tool_calls=[call])
        assert read_tool_args(resp) == {"key": "value", "n": 42}

    def test_returns_first_call_args_when_multiple(self):
        """Defined behavior: the helper reads only the first tool call.
        Multi-tool callers must not depend on this — they should iterate
        explicitly."""
        c1 = SimpleNamespace(arguments={"first": True})
        c2 = SimpleNamespace(arguments={"second": True})
        resp = SimpleNamespace(success=True, tool_calls=[c1, c2])
        assert read_tool_args(resp) == {"first": True}

    def test_returns_none_when_success_false(self):
        """Dispatch failure / retry exhausted — never trust the args even
        if the response shape happens to carry some."""
        call = SimpleNamespace(arguments={"would_be_value": 1})
        resp = SimpleNamespace(success=False, tool_calls=[call])
        assert read_tool_args(resp) is None

    def test_returns_none_when_tool_calls_empty(self):
        resp = SimpleNamespace(success=True, tool_calls=[])
        assert read_tool_args(resp) is None

    def test_returns_none_when_tool_calls_missing(self):
        """Pre-W.6 ModelResponse shapes lacked `tool_calls` entirely —
        the getattr guard handles that case without raising."""
        resp = SimpleNamespace(success=True)
        assert read_tool_args(resp) is None

    def test_returns_none_when_tool_calls_attr_is_none(self):
        """Some provider error paths may set tool_calls=None explicitly."""
        resp = SimpleNamespace(success=True, tool_calls=None)
        assert read_tool_args(resp) is None

    def test_returns_none_when_args_not_dict(self):
        """Pathological provider return shape — args is a list/string.
        Defends downstream callers expecting `args.get(...)`."""
        call_list = SimpleNamespace(arguments=["not", "a", "dict"])
        resp_list = SimpleNamespace(success=True, tool_calls=[call_list])
        assert read_tool_args(resp_list) is None

        call_str = SimpleNamespace(arguments="raw string")
        resp_str = SimpleNamespace(success=True, tool_calls=[call_str])
        assert read_tool_args(resp_str) is None

    def test_returns_none_on_completely_missing_success(self):
        """getattr default is False, so a response missing the attribute
        is treated as failure (defensive — some test stubs leave it off)."""
        resp = SimpleNamespace(tool_calls=[SimpleNamespace(arguments={"x": 1})])
        assert read_tool_args(resp) is None
