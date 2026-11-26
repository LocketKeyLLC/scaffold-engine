"""§17.2025 — a defect the code reviewer names is a FAIL, however its verdict reads.

Live (follow-up 5b4880c0 T6, 2026-10-26, §17.1829): DONE with the verifier's PASS at 0.62, "residual risk is only the
undocumented 'equity' context key" -- a key the data module never returns, so in production the step proposed
nothing. The reviewer had found the defect and wrote it into a passing reason. Of the 23 code passes in the 14 days'
traces (2026-11-26), trace 13588 passed with "only a cosmetic flaw exists (the chart-fallback message reads
canvas.dataset.vendor, which is never set, printing \"undefined\")".

Now the code reviewer's tool carries `defects`, a list the engine reads: a PASS listing any is recorded as a FAIL
naming them. The generic (lenient) verifier is unchanged.
"""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.modules import execution_verify as ev

MSGS = [{"role": "user", "content": "TASK: entry half\n\nOUTPUT:\nx = 1"}]


def _resp(args: dict) -> SimpleNamespace:
    return SimpleNamespace(success=True, text="", error=None, tool_calls=[SimpleNamespace(arguments=args)])


class _NoCache:
    async def get(self, *a):
        return None

    async def put(self, *a):
        return None


async def _verdicts(responses, tool) -> tuple[list[tuple], list]:
    call = AsyncMock(side_effect=responses)
    with patch.object(ev.model_router, "tool_call", new=call), \
         patch.object(ev, "get_verifier_cache", return_value=_NoCache()):
        out = [await ev._run_verification(MSGS, tool=tool) for _ in responses]
    return out, [c.kwargs["tools"][0] for c in call.call_args_list]


async def test_a_pass_that_lists_defects_is_a_fail_naming_them():
    replies = [
        # §17.1829's verdict, with the defect it named listed (and once more, and a "none" beside it)
        _resp({"pass": True, "confidence": 0.62,
               "reason": "Implements the entry half; residual risk is only the undocumented 'equity' context key",
               "defects": ["reads context['equity']; load_batch_context never returns 'equity'",
                           "skips every signal on signal.get('id'), a key no signal carries",
                           "reads context['equity']; load_batch_context never returns 'equity'", "none"]}),
        # trace 13588's shape
        _resp({"pass": True, "confidence": 0.82, "reason": "The template delivers everything this step requires",
               "defects": "the chart-fallback message reads canvas.dataset.vendor, which is never set"}),
        _resp({"pass": True, "confidence": 0.9, "reason": "complete", "defects": []}),
        _resp({"pass": True, "confidence": 0.9, "reason": "complete; assumes the page uses window.fetch",
               "defects": ["None"]}),
        _resp({"pass": False, "confidence": 0.8, "reason": "missing the writer", "defects": ["no writer"]}),
        _resp({"pass": True, "confidence": 0.7, "reason": "a written verdict carries no list"}),
    ]
    out, tools = await _verdicts(replies, ev.CODEGEN_VERIFY_TOOL)
    assert [s for s, _r, _c in out] == ["fail", "fail", "pass", "pass", "fail", "pass"]
    assert out[0][1] == (
        "The reviewer passed it but named defect(s) in the code: reads context['equity']; load_batch_context never "
        "returns 'equity'; skips every signal on signal.get('id'), a key no signal carries (its verdict: Implements "
        "the entry half; residual risk is only the undocumented 'equity' context key)")
    assert out[0][2] == 0.62
    assert "canvas.dataset.vendor" in out[1][1]
    assert out[4][1] == "missing the writer"                        # a FAIL keeps its own reason
    assert all(t is ev.CODEGEN_VERIFY_TOOL for t in tools)


async def test_the_lenient_verifier_ignores_a_list_its_tool_never_asks_for():
    out, tools = await _verdicts([_resp({"pass": True, "confidence": 0.9, "reason": "ok", "defects": ["x"]}),
                                  _resp({"pass": True, "confidence": 0.9, "reason": "ok"})], ev.VERIFY_TOOL)
    assert [s for s, _r, _c in out] == ["pass", "pass"] and "defects" not in tools[0].input_schema["properties"]


async def test_the_code_verifier_asks_for_the_list_and_says_a_named_defect_fails():
    run = AsyncMock(return_value=("pass", "ok", 0.9))
    with patch.object(ev, "_run_verification", new=run):
        await ev._verify_codegen_output("Entry half", "```python\nx = 1\n```", brief_goal="Propose trades.")
    assert run.await_args.kwargs["tool"] is ev.CODEGEN_VERIFY_TOOL
    schema = ev.CODEGEN_VERIFY_TOOL.input_schema
    assert "defects" in schema["required"] and schema["properties"]["defects"]["type"] == "array"
    system = run.await_args.args[0][0]["content"]
    assert 'Never PASS with a "residual risk", a "caveat" or a "minor flaw" that is a defect' in system
    assert "is not a defect: leave it out of `defects`" in system
