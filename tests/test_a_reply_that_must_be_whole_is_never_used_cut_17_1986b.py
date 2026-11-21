"""§17.1986b — a reply that is only usable whole (JSON parsed from text, code, a script) and stopped at its token
limit is redrawn once at a larger budget and, still cut, refused — never used cut. Free prose is left as it was.

Live: llm_traces 13038/13104/13193/13219/13231/13234/13247, the runbook template's script fill at 1500/1500 (each
caught by its fence count and redrawn at 4500 — a cut with no fence would not have been); 13356, a CodeGen node
draw cut at 8192/8192, where §17.1792's gate read `resp.completion_tokens`, a field ModelResponse does not have."""
import logging
from unittest.mock import AsyncMock, patch

import pytest

from app import model_router
from app.modules import runbook_templates as rt
from app.providers.base import ModelResponse
from app.utils.cut_reply import cut_at

# 13104's reply, as a model that writes no fence would have sent it: cut mid-command at the limit
CUT_SCRIPT = ("set -u\ncurl -s -o /dev/null -w 'caddy %{http_code} %{time_total}\\n' -m 3 http://127.0.0.1:2019/config/\n"
              "curl -s -o /dev/null -w 'media-request %{http_code} %{time_total}\\n' -m 3 -")
WHOLE_SCRIPT = CUT_SCRIPT + "u \"panel:$PANEL_PASSWORD\" http://127.0.0.1:3001/api/media/request"


class _Provider:
    name = "fake"
    supports_native_tools = False

    def __init__(self, replies):
        self.replies = list(replies)
        self.budgets: list[int] = []

    def _next(self, max_tokens):
        self.budgets.append(max_tokens)
        text, reason = self.replies.pop(0)
        used = max_tokens if reason == "length" else max_tokens // 3
        return ModelResponse(model="m", success=True, text=text, tokens_completion=used, raw={"done_reason": reason})

    async def generate(self, model, prompt, *, max_tokens, **kw):
        return self._next(max_tokens)

    async def chat_completion(self, model, messages, *, max_tokens, **kw):
        return self._next(max_tokens)


def _routed(p):
    return (patch.object(model_router, "_resolve_role", lambda role, ov: ("m", p)),
            patch.object(model_router, "_record_call", AsyncMock(side_effect=lambda r: r)))


async def test_a_whole_reply_cut_once_is_redrawn_larger():
    p = _Provider([(CUT_SCRIPT, "length"), (WHOLE_SCRIPT, "stop")])
    a, b = _routed(p)
    with a, b:
        resp = await model_router.generate("fill", role="model_general", max_tokens=1500, whole=True)
    assert resp.success and resp.text == WHOLE_SCRIPT and p.budgets == [1500, 3000]


async def test_a_whole_reply_still_cut_is_refused_loudly(caplog):
    p = _Provider([(CUT_SCRIPT, "length"), (CUT_SCRIPT, "length")])
    a, b = _routed(p)
    with a, b, caplog.at_level(logging.ERROR, logger="scaffold.router"):
        resp = await model_router.chat([{"role": "user", "content": "x"}], role="model_general",
                                       max_tokens=1500, whole=True)
    assert resp.success is False and "§17.1986" in resp.error and cut_at(resp) == 3000
    assert "whole_reply_cut_refused" in caplog.text


async def test_a_whole_reply_that_stopped_is_drawn_once():
    p = _Provider([(WHOLE_SCRIPT, "stop")])
    a, b = _routed(p)
    with a, b:
        resp = await model_router.generate("fill", role="model_general", max_tokens=1500, whole=True)
    assert resp.success and p.budgets == [1500]


async def test_cut_prose_is_left_as_it_was():
    # a guide or a report cut at its limit is a long answer, not a broken object: no whole=, no redraw
    p = _Provider([("## Do this next\nRun the check, then", "length")])
    a, b = _routed(p)
    with a, b:
        resp = await model_router.chat([{"role": "user", "content": "x"}], role="model_general", max_tokens=8192)
    assert resp.success and p.budgets == [8192]


# ── the runbook template's script fill (the 7 live 1500/1500 replies) ─────────

def _fake_fill(replies, budgets):
    async def fake(gen, prompt, params, *, system, max_tokens, **kw):
        budgets.append(max_tokens)
        if "VERIFY_INSIDE" in kw.get("label", ""):
            return ModelResponse(text="curl -s http://127.0.0.1:3001/api/health", raw={"done_reason": "stop"})
        text, reason = replies.pop(0)
        return ModelResponse(text=text, tokens_completion=max_tokens, raw={"done_reason": reason})
    return fake


NODE = {"node_key": "T16", "title": "Proxmox host: test the control panel routes in CT 111", "description": ""}


async def test_an_unfenced_cut_script_is_redrawn_and_never_used(monkeypatch):
    import app.utils.llm_retry as lr
    budgets: list[int] = []
    monkeypatch.setattr(lr, "generate_until_nonempty", _fake_fill([(CUT_SCRIPT, "length"), (CUT_SCRIPT, "length")], budgets))
    assert not rt.content_is_cut(CUT_SCRIPT)          # the fence/heredoc reading alone passes it
    with pytest.raises(rt.ContentCut):
        await rt.fill_free_params(rt.RUN_IN_CONTAINER, NODE, "brief")
    assert budgets == [1500, 4500]


async def test_an_unfenced_cut_script_redrawn_whole_is_used(monkeypatch):
    import app.utils.llm_retry as lr
    budgets: list[int] = []
    monkeypatch.setattr(lr, "generate_until_nonempty", _fake_fill([(CUT_SCRIPT, "length"), (WHOLE_SCRIPT, "stop")], budgets))
    vals = await rt.fill_free_params(rt.RUN_IN_CONTAINER, NODE, "brief")
    assert vals["REMOTE_COMMANDS"].endswith("/api/media/request") and budgets[:2] == [1500, 4500]


async def test_a_whole_script_is_drawn_once(monkeypatch):
    import app.utils.llm_retry as lr
    budgets: list[int] = []
    monkeypatch.setattr(lr, "generate_until_nonempty", _fake_fill([(WHOLE_SCRIPT, "stop")], budgets))
    vals = await rt.fill_free_params(rt.RUN_IN_CONTAINER, NODE, "brief")
    assert vals["REMOTE_COMMANDS"].endswith("/api/media/request") and budgets[0] == 1500 and 4500 not in budgets


# ── generate(...) + parse_json_object(...) callers ──────────────────────────

async def test_a_cut_validator_verdict_is_redrawn_not_repaired():
    from app.modules.dag_validator import validate_tool_picks
    cut = '{"issues": [{"node_id": "T2", "current_tool": "CodeGen", "proposed_tool": "LLM", "reason": "Documentation is'
    whole = '{"issues": [{"node_id": "T2", "current_tool": "CodeGen", "proposed_tool": "LLM", "reason": "Documentation is not code."}]}'
    p = _Provider([(cut, "length"), (whole, "stop")])
    a, b = _routed(p)
    with a, b:
        out = await validate_tool_picks([{"id": "T1", "name": "A", "tool": "LLM"}, {"id": "T2", "name": "Docs", "tool": "CodeGen"}],
                                        max_tokens=3072)
    assert out.error is None and [i.reason for i in out.issues] == ["Documentation is not code."]
    assert p.budgets == [3072, 6144]


async def test_a_validator_verdict_still_cut_is_an_error_not_a_stump():
    from app.modules.dag_validator import validate_tool_picks
    cut = '{"issues": [{"node_id": "T2", "current_tool": "CodeGen", "proposed_tool": "LLM", "reason": "Documentation is'
    p = _Provider([(cut, "length"), (cut, "length")])
    a, b = _routed(p)
    with a, b:
        out = await validate_tool_picks([{"id": "T2", "name": "Docs", "tool": "CodeGen"}], max_tokens=3072)
    assert out.issues == [] and out.error and "§17.1986" in out.error


def test_every_text_json_caller_asks_for_a_whole_reply():
    # the generate/chat + parse_json_object callers (the shapes generate_json / tool_call do not cover)
    import inspect
    from app.modules import dag_generator, dag_validator
    from app.sim import device_sizing, digital_sizing, formal_verify, spec_extractor, topology_select
    for mod in (device_sizing, digital_sizing, formal_verify, spec_extractor, topology_select):
        assert '{"role": role, "whole": True}' in inspect.getsource(mod), mod.__name__
    assert inspect.getsource(dag_validator).count("whole=True") == 1
    assert inspect.getsource(dag_generator).count("whole=True") == 2


def test_the_response_has_no_completion_tokens_field():
    # §17.1792's gate read this name; the real one is tokens_completion, so the gate never fired live
    assert not hasattr(ModelResponse(), "completion_tokens") and hasattr(ModelResponse(), "tokens_completion")


# ── a streamed node draw (node_token_streaming_enabled; OFF live, covered anyway) ──

async def test_ollama_stream_reports_its_final_stop_reason():
    from unittest.mock import MagicMock
    from app.providers.ollama import OllamaProvider
    from app.utils.cut_reply import stream_was_cut
    from tests.test_providers import _fake_ollama_stream
    for last, expected in (('{"message":{"content":""},"done":true,"done_reason":"length","eval_count":8192}', True),
                           ('{"message":{"content":""},"done":true,"done_reason":"stop","eval_count":40}', False)):
        client = MagicMock()
        client.stream = _fake_ollama_stream(lines=['{"message":{"content":"set -u\\ncurl -s"}}', last])
        stop: dict = {}
        with patch.object(model_router, "_get_client", return_value=client):
            chunks = [c async for c in OllamaProvider().stream_chat("m", [{"role": "user", "content": "x"}],
                                                                    max_tokens=8192, stop_info=stop)]
        assert chunks == ["set -u\ncurl -s"]
        assert stream_was_cut(stop, 8192) is expected, stop


from tests.test_execution_agent_token_stream import _node_row as _llm_row   # noqa: E402 — bound before any patch


def _shell_node():
    row = _llm_row()
    row.update(tool="Shell", title="Check the panel routes in CT 111", prompt_template="Check the panel routes.")
    return row


def _stream_ending(reason, used):
    async def _stream(messages, stop_info=None, **kw):
        yield "```bash\nset -u\ncurl -s -o /dev/null -w '%{http_code}' -m 3 -"
        if stop_info is not None:
            stop_info.update(done_reason=reason, eval_count=used)
    return _stream


async def _drive_shell(stream, chat_mock):
    import asyncio
    from tests import test_execution_agent_token_stream as ts
    with patch.object(ts, "_node_row", _shell_node):
        await ts._drive("4e3b8f01-145c-4c54-a0f6-5639101ee1ca", stream_mock=stream, chat_mock=chat_mock,
                        token_q=asyncio.Queue())


async def test_a_cut_streamed_script_is_redrawn_whole_not_kept(caplog):
    from tests.test_execution_agent_token_stream import _ok
    chat_mock = AsyncMock(return_value=_ok("```bash\nset -u\ncurl -s http://127.0.0.1:3001/api/health\n```"))
    with caplog.at_level(logging.WARNING):
        await _drive_shell(_stream_ending("length", 8192), chat_mock)
    assert chat_mock.await_count >= 1 and chat_mock.await_args.kwargs.get("whole") is True
    assert "node_token_stream_cut" in caplog.text


async def test_a_streamed_script_that_ended_is_kept():
    from tests.test_execution_agent_token_stream import _ok
    chat_mock = AsyncMock(return_value=_ok("unused"))
    await _drive_shell(_stream_ending("stop", 60), chat_mock)
    assert chat_mock.await_count == 0
