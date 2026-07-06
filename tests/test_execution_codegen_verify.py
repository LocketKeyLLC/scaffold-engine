"""§17.429 — unit tests for the stricter CodeGen verifier.

Covers extract_brief_goal, collect_upstream_code, _verify_codegen_output
(message construction + verdict parse + fail-closed), and a regression
guard that the generic _verify_output's messages are unchanged.

Offline: model_router.tool_call and the verifier cache are both patched.
"""
from unittest.mock import AsyncMock, patch

import pytest

from app.modules import execution_verify
from app.modules.execution_verify import (
    CODEGEN_VERIFY_SYSTEM,
    VERIFY_SYSTEM,
    _verify_codegen_output,
    _verify_output,
    collect_upstream_code,
    extract_brief_goal,
)
from app.providers.base import ModelResponse, ToolCall

pytestmark = pytest.mark.smoke


def _ok(args: dict) -> ModelResponse:
    return ModelResponse(
        text="", model="fake", success=True,
        tool_calls=[ToolCall(id="t0", name="record_verification", arguments=args)],
    )


class _NoCache:
    """Force cache misses so every call reaches the mocked tool_call."""
    async def get(self, *a):
        return None

    async def put(self, *a):
        return None


@pytest.fixture
def patch_tool_call():
    with patch.object(execution_verify, "get_verifier_cache", return_value=_NoCache()), \
         patch.object(execution_verify.model_router, "tool_call", new=AsyncMock()) as m:
        yield m


# ---------------------------------------------------------------------------
# extract_brief_goal
# ---------------------------------------------------------------------------

def test_brief_goal_description():
    assert extract_brief_goal({"description": "build X"}) == "build X"


def test_brief_goal_falls_back_to_goals():
    assert extract_brief_goal({"goals": ["first", "second"]}) == "first"


def test_brief_goal_empty():
    assert extract_brief_goal(None) == ""
    assert extract_brief_goal({}) == ""
    assert extract_brief_goal({"description": ""}) == ""


# ---------------------------------------------------------------------------
# collect_upstream_code
# ---------------------------------------------------------------------------

def test_collect_keeps_only_python_blocks():
    ups = {
        "T1": "prose only, no code",
        "T2": "```python\ndef f(): ...\n```",
        "T3": "```js\nconst x = 1;\n```",
    }
    got = collect_upstream_code(ups)
    assert [k for k, _ in got] == ["T2"]


def test_collect_truncates_long_blocks():
    big = "x = 1\n" * 1000
    got = collect_upstream_code({"T1": f"```python\n{big}\n```"}, per_block_cap=50)
    assert len(got) == 1
    assert "truncated" in got[0][1]


def test_collect_empty():
    assert collect_upstream_code(None) == []
    assert collect_upstream_code({}) == []
    assert collect_upstream_code({"T1": ""}) == []


# §17.514 — regression: _fetch_upstream_outputs returns (output_text, confidence)
# TUPLES (since §17.477), not strings. collect_upstream_code must unpack the
# text. Pre-fix, passing the tuple to extract_code_blocks raised
# "TypeError: expected string or bytes-like object, got 'tuple'", crashing every
# CodeGen node with upstream deps under the strict verifier. The prior tests
# only used string values, so they never caught the production shape.

def test_collect_handles_tuple_values_from_fetch_upstream():
    ups = {
        "T1": ("prose only, no code", 0.9),
        "T2": ("```python\ndef f(): ...\n```", None),
        "T3": ("```js\nconst x = 1;\n```", 0.5),
    }
    got = collect_upstream_code(ups)
    assert [k for k, _ in got] == ["T2"]
    assert "def f()" in got[0][1]


def test_collect_tuple_with_empty_text():
    assert collect_upstream_code({"T1": ("", 0.8)}) == []


# ---------------------------------------------------------------------------
# _verify_codegen_output
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uses_codegen_system_and_includes_context(patch_tool_call):
    patch_tool_call.return_value = _ok({"pass": True, "reason": "ok", "confidence": 0.9})
    status, reason, conf = await _verify_codegen_output(
        "Write parser",
        "```python\ndef parse(): ...\n```",
        brief_goal="build a config parser",
        upstream_code=[("T1", "def helper(): ...")],
    )
    assert status == "pass"
    msgs = patch_tool_call.call_args.kwargs["messages"]
    assert msgs[0]["content"] == CODEGEN_VERIFY_SYSTEM
    user = msgs[1]["content"]
    assert "build a config parser" in user
    assert "upstream T1" in user
    assert "OUTPUT TO REVIEW" in user


@pytest.mark.asyncio
async def test_omits_empty_brief_and_upstream_sections(patch_tool_call):
    patch_tool_call.return_value = _ok({"pass": True, "reason": "ok", "confidence": 0.9})
    await _verify_codegen_output("t", "```python\nx = 1\n```")
    user = patch_tool_call.call_args.kwargs["messages"][1]["content"]
    assert "PROJECT GOAL" not in user
    assert "UPSTREAM CODE" not in user


@pytest.mark.asyncio
async def test_fail_verdict_propagates_reason(patch_tool_call):
    patch_tool_call.return_value = _ok(
        {"pass": False, "reason": "signature drift on render_table", "confidence": 0.8}
    )
    status, reason, conf = await _verify_codegen_output("t", "```python\nx = 1\n```")
    assert status == "fail"
    assert "signature drift" in reason


@pytest.mark.asyncio
async def test_fail_closed_on_exception(patch_tool_call):
    patch_tool_call.side_effect = RuntimeError("boom")
    status, reason, conf = await _verify_codegen_output("t", "```python\nx = 1\n```")
    assert status == "fail"
    assert conf == 0.0


# ---------------------------------------------------------------------------
# regression — generic verifier is byte-identical post-refactor
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generic_verify_messages_unchanged(patch_tool_call):
    patch_tool_call.return_value = _ok({"pass": True, "reason": "ok", "confidence": 0.9})
    await _verify_output("List 3 things", "a, b, c")
    msgs = patch_tool_call.call_args.kwargs["messages"]
    assert msgs[0]["content"] == VERIFY_SYSTEM
    assert msgs[1]["content"] == "TASK: List 3 things\n\nOUTPUT:\na, b, c"
