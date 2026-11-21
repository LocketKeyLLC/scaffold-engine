"""§17.1986 — a structured reply cut at its token budget is redrawn, then its stump dropped or refused — never parsed
into a "whole" object. Live shape: llm_traces 13441/13442 (the refine draw: native call empty at 2048/2048, then the
coaxed reply cut mid-question, json_repair closing "(b) a summary you can click to")."""
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest

from app import model_router
from app.modules.idea_refinement import REFINE_BRIEF_TOOL
from app.providers.base import ModelResponse, ToolCall
from app.utils.cut_reply import drop_cut_tail, redraw_budget, reply_was_cut

STUMP = "(b) a summary you can click to"

# 13442's reply, its long fields trimmed, the cut tail exactly as the model wrote it (no closing quote/bracket).
CUT_BRIEF = (
    '{"title":"Paper-Trading Lab Operator Dashboard","description":"Add a read-only operator dashboard to the '
    'paper-trading lab already living in the engine.","domain":"eng","goals":["Add an HTML dashboard route at '
    '/labs/paper/dashboard.","Display the full audit trail."],"constraints":["Do not alter the behaviour or auth of '
    'existing /labs/paper routes."],"complexity":"medium","ambiguities":["How should costs be shown next to returns? '
    '(a) A single net figure; (b) the net figure plus the gross figure; (c) net figure plus a small cost breakdown '
    'per period.","How much of each proposal\'s rationale and sources should be on the page? (a) A one-line summary '
    'per proposal — fits many proposals on screen; ' + STUMP
)
WHOLE_BRIEF = CUT_BRIEF + ' open for the full rationale; (c) everything shown in full."]}'


class _Provider:
    """Native tool call comes back empty and cut (13441); the coaxed chat replies come from ``coaxed``."""
    name = "fake"
    supports_native_tools = True

    def __init__(self, coaxed, native=None):
        self.coaxed = list(coaxed)
        self.native = native
        self.budgets: list[int] = []

    async def tool_call(self, model, messages, tools, *, temperature, max_tokens, tool_choice, **kw):
        self.budgets.append(max_tokens)
        if self.native is not None:
            return self.native(max_tokens)
        return ModelResponse(model=model, success=True, text="", tokens_completion=max_tokens,
                             raw={"done_reason": "length", "message": {"content": ""}})

    async def chat_completion(self, model, messages, *, temperature, max_tokens, fallback=None, **kw):
        self.budgets.append(max_tokens)
        text, reason = self.coaxed.pop(0)
        used = max_tokens if reason == "length" else max_tokens // 2
        return ModelResponse(model=model, success=True, text=text, tokens_completion=used,
                             raw={"done_reason": reason, "message": {"content": text}})


async def _call(provider, tool=REFINE_BRIEF_TOOL, max_tokens=2048):
    with patch.object(model_router, "_resolve_role", lambda role, ov: ("deepseek-v4.1-flash:cloud", provider)), \
         patch.object(model_router, "_record_call", AsyncMock(side_effect=lambda r: r)):
        return await model_router.tool_call(
            [{"role": "user", "content": "refine"}], [tool], role="model_general",
            temperature=0.3, max_tokens=max_tokens,
        )


async def test_the_live_refine_cut_is_redrawn_at_twice_the_budget(caplog):
    p = _Provider([(CUT_BRIEF, "length"), (WHOLE_BRIEF, "stop")])
    with caplog.at_level(logging.WARNING, logger="scaffold.router"):
        resp = await _call(p)
    args = resp.tool_calls[0].arguments
    assert args["ambiguities"][-1].endswith("(c) everything shown in full.")
    assert p.budgets == [2048, 2048, 4096, 4096]          # native + coax at 2048, then both at 4096
    assert "structured_reply_cut_redraw" in caplog.text


async def test_still_cut_loses_only_the_stump(caplog):
    p = _Provider([(CUT_BRIEF, "length"), (CUT_BRIEF, "length")])
    with caplog.at_level(logging.WARNING, logger="scaffold.router"):
        resp = await _call(p)
    args = resp.tool_calls[0].arguments
    assert resp.success and len(args["ambiguities"]) == 1
    assert not any(STUMP in a for a in args["ambiguities"])
    assert args["title"] == "Paper-Trading Lab Operator Dashboard" and len(args["goals"]) == 2
    assert "structured_reply_cut_tail_dropped" in caplog.text


async def test_a_cut_research_query_is_never_searched():
    # second caller: assist_research_lib._detect_unknowns (flag_unknowns, max_tokens=1024; live completions up to
    # 959), trace 13135's coaxed shape cut inside its second query
    from app.modules import assist_research_lib as arl
    cut = ('{"queries": ["Caddy 2 reverse_proxy default dial_timeout / response_header_timeout values", '
           '"Caddy reverse_proxy upstream hangs until')
    p = _Provider([(cut, "length"), (cut, "length")])
    with patch.object(model_router, "_resolve_role", lambda role, ov: ("deepseek-v4.1-flash:cloud", p)), \
         patch.object(model_router, "_record_call", AsyncMock(side_effect=lambda r: r)):
        queries = await arl._detect_unknowns(task_text="put Caddy in front of the panel", tool="Shell",
                                             role="model_verifier", max_queries=3)
    assert queries == ["Caddy 2 reverse_proxy default dial_timeout / response_header_timeout values"]
    assert p.budgets[-1] == 2048


async def test_a_cut_inside_a_scalar_is_refused_loudly(caplog):
    cut = '{"title":"Paper-Trading Lab Operator Dashboard","description":"Add a read-only operator dash'
    p = _Provider([(cut, "length"), (cut, "length")])
    with caplog.at_level(logging.ERROR, logger="scaffold.router"):
        resp = await _call(p)
    assert resp.success is False and resp.tool_calls == [] and "§17.1986" in resp.error
    assert "structured_reply_cut_refused" in caplog.text


async def test_a_reply_that_stopped_normally_is_drawn_once():
    whole = json.loads(WHOLE_BRIEF)
    p = _Provider([(WHOLE_BRIEF, "stop")])
    resp = await _call(p)
    assert resp.tool_calls[0].arguments == whole and p.budgets == [2048, 2048]


async def test_a_native_call_is_strictly_parsed_and_not_redrawn():
    # a native tool call is parsed by the provider whole or not at all — there is no repaired stump to drop
    def native(budget):
        return ModelResponse(success=True, tokens_completion=budget, raw={"done_reason": "length"},
                             tool_calls=[ToolCall(id="tool_0", name="emit_refined_brief",
                                                  arguments=json.loads(WHOLE_BRIEF))])
    p = _Provider([], native=native)
    resp = await _call(p)
    assert p.budgets == [2048] and len(resp.tool_calls[0].arguments["ambiguities"]) == 2


async def test_a_cut_free_text_reply_is_not_structured():
    # free text (a guide, a script) is not parsed into an object: generate leaves it as the caller asked
    p = _Provider([("```bash\nset -u\nOUT=/tmp/t16.txt\ncurl -s -o /dev/null", "length")])
    with patch.object(model_router, "_resolve_role", lambda role, ov: ("m", p)), \
         patch.object(model_router, "_record_call", AsyncMock(side_effect=lambda r: r)):
        resp = await model_router.chat([{"role": "user", "content": "fill"}], role="model_general", max_tokens=1500)
        assert resp.text.endswith("/dev/null") and p.budgets == [1500]
        # no tools: nothing is parsed, so nothing is redrawn — one draw, returned as is
        plain = await model_router.tool_call([{"role": "user", "content": "x"}], [], role="model_general",
                                             max_tokens=1500)
    assert plain.success and plain.tool_calls == [] and p.budgets == [1500, 1500]


async def test_generate_json_drops_the_cut_item_of_an_array(caplog):
    cut = '[{"symbol": "SPY", "side": "buy"}, {"symbol": "QQQ", "side": "se'
    calls = []

    async def fake_generate(prompt, **kw):
        calls.append(kw["max_tokens"])
        return ModelResponse(success=True, text=cut, tokens_completion=kw["max_tokens"],
                             raw={"done_reason": "length"})
    with patch.object(model_router, "generate", fake_generate), \
         caplog.at_level(logging.WARNING, logger="scaffold.router"):
        parsed, _ = await model_router.generate_json("p", {"type": "array"}, role="model_general",
                                                     max_tokens=3000, as_array=True)
    assert parsed == [{"symbol": "SPY", "side": "buy"}] and calls == [3000, 6000]
    assert "structured_reply_cut_tail_dropped" in caplog.text


async def test_generate_json_a_whole_reply_is_drawn_once():
    calls = []

    async def fake_generate(prompt, **kw):
        calls.append(kw["max_tokens"])
        return ModelResponse(success=True, text='{"a": [1, 2]}', tokens_completion=kw["max_tokens"],
                             raw={"done_reason": "stop"})
    with patch.object(model_router, "generate", fake_generate):
        parsed, _ = await model_router.generate_json("p", {"type": "object"}, role="model_general", max_tokens=500)
    assert parsed == {"a": [1, 2]} and calls == [500]


@pytest.mark.parametrize("raw,used,expected", [
    ({"done_reason": "length"}, 10, True),
    ({"done_reason": "stop"}, 2048, False),                 # a stated normal stop wins over the count
    ({"choices": [{"finish_reason": "length"}]}, 1, True),  # OpenAI
    ({"stop_reason": "max_tokens"}, 1, True),               # Anthropic
    ({"stop_reason": "end_turn"}, 2048, False),
    ({}, 2048, True),                                       # no reason known: the count decides
    ({}, 2047, False),
])
def test_reply_was_cut(raw, used, expected):
    assert reply_was_cut(ModelResponse(raw=raw, tokens_completion=used), 2048) is expected


def test_the_redraw_is_bounded_by_the_cap():
    assert redraw_budget(2048, 2048, 16384) == 4096
    assert redraw_budget(1024, 4096, 16384) == 8192        # a coax floor raised the real budget
    assert redraw_budget(16384, 16384, 16384) == 16384     # no room: settle without a redraw


async def test_at_the_cap_it_settles_without_a_redraw():
    from app.config import settings
    cap = settings.structured_cut_redraw_max_tokens
    p = _Provider([(CUT_BRIEF, "length")])
    resp = await _call(p, max_tokens=cap)
    assert p.budgets == [cap, cap] and len(resp.tool_calls[0].arguments["ambiguities"]) == 1


def test_drop_cut_tail_shapes():
    assert drop_cut_tail({"a": "x", "b": [1, 2]}) == ({"a": "x", "b": [1]}, 2)
    assert drop_cut_tail({"a": [1], "b": {"c": "d", "e": ["f", "g"]}}) == ({"a": [1], "b": {"c": "d", "e": ["f"]}}, "g")
    assert drop_cut_tail({"a": [1], "b": "cut"}) == (None, None)
    assert drop_cut_tail({"a": "x", "b": []}) == (None, None)
