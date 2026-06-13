"""§17.486 — unit tests for the Assist Mode guidance layer.

The model and the grounding helpers are always mocked: never rely on a real
LLM draw (the cloud thinking models can return success=True + empty content,
§17.465 — the empty-pitfall tests below assert chat_until_nonempty re-draws).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_guide
from app.modules.prompt_assembly import (
    EXECUTION_SYSTEM_RUNBOOK,
    StepContext,
)


# ── helpers ──────────────────────────────────────────────────────────────


def _ctx(tool: str = "shell", *, upstream=None) -> StepContext:
    return StepContext(
        node_key="T3",
        title="Install the proxy",
        tool=tool,
        domain=None,
        system_prompt="sys",
        base_prompt="Install and start the reverse proxy on the host.",
        upstream_outputs=upstream or {},
        upstream_truncated_keys=[],
        grounding="",
        grounding_kind=None,
        assembled_prompt="Install and start the reverse proxy on the host.",
    )


def _resp(text: str, success: bool = True, error: str | None = None):
    r = MagicMock()
    r.success = success
    r.text = text
    r.error = error
    r.model = "fake-model"
    return r


def _tool_resp(queries, success: bool = True):
    r = MagicMock()
    r.success = success
    if success:
        call = MagicMock()
        call.arguments = {"queries": queries}
        r.tool_calls = [call]
    else:
        r.tool_calls = []
    return r


def _db():
    db = AsyncMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()
    return db


# ── system-prompt routing ────────────────────────────────────────────────


def test_guide_system_for_tool_shell_reuses_runbook():
    s = assist_guide.guide_system_for_tool("shell")
    assert EXECUTION_SYSTEM_RUNBOOK in s
    assert "human operator" in s.lower()


def test_guide_system_for_tool_codegen():
    assert assist_guide.guide_system_for_tool("codegen") is assist_guide.GUIDE_SYSTEM_CODEGEN


def test_guide_system_for_tool_defaults_to_noncode():
    assert assist_guide.guide_system_for_tool("LLM") is assist_guide.GUIDE_SYSTEM_NONCODE
    assert assist_guide.guide_system_for_tool(None) is assist_guide.GUIDE_SYSTEM_NONCODE


# ── generation (happy path) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_guidance_ready_no_research():
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("## Run this\n1. do it"))) as chat:
        res = await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=False, node_key="T3",
        )
    assert res["status"] == "ready"
    assert res["guidance"].startswith("## Run this")
    assert res["guidance_meta"]["research_sources"] == []
    assert res["guidance_meta"]["tool"] == "shell"
    chat.assert_awaited()  # generation happened


@pytest.mark.asyncio
async def test_generate_guidance_research_false_skips_helpers():
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("walk"))), \
         patch("app.modules.execution_agent._searxng_search", new=AsyncMock()) as sx, \
         patch("app.modules.execution_agent._milvus_search", new=AsyncMock()) as mv, \
         patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()) as tc:
        await assist_guide.generate_guidance(
            ctx=_ctx("codegen"), research=False, node_key="T3",
        )
    sx.assert_not_called()
    mv.assert_not_called()
    tc.assert_not_called()


# ── empty-pitfall (§17.465) ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_guidance_redraws_on_empty_then_succeeds():
    chat = AsyncMock(side_effect=[_resp(""), _resp(""), _resp("finally")])
    with patch.object(assist_guide.model_router, "chat", new=chat):
        res = await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=False, node_key="T3",
        )
    assert res["status"] == "ready"
    assert res["guidance"] == "finally"
    assert chat.await_count == 3  # re-drew past the two empties


@pytest.mark.asyncio
async def test_generate_guidance_all_empty_marks_failed():
    chat = AsyncMock(return_value=_resp(""))
    with patch.object(assist_guide.model_router, "chat", new=chat):
        res = await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=False, node_key="T3",
        )
    assert res["status"] == "failed"
    assert res["guidance"] == ""
    assert "error" in res["guidance_meta"]


@pytest.mark.asyncio
async def test_generate_guidance_hard_failure_marks_failed():
    chat = AsyncMock(return_value=_resp("", success=False, error="model down"))
    with patch.object(assist_guide.model_router, "chat", new=chat):
        res = await assist_guide.generate_guidance(
            ctx=_ctx("LLM"), research=False, node_key="T3",
        )
    assert res["status"] == "failed"
    assert res["guidance_meta"]["error"] == "model down"


# ── research pre-pass ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_research_prepass_collects_and_injects_sources():
    captured = {}

    async def _capture_chat(messages, **kw):
        captured["user"] = messages[1]["content"]
        return _resp("guided")

    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_tool_resp(["nginx install ubuntu"]))), \
         patch("app.modules.execution_agent._milvus_search",
               new=AsyncMock(return_value="No knowledge base results found.")), \
         patch("app.modules.execution_agent._searxng_search",
               new=AsyncMock(return_value="[1] nginx docs\n    apt install nginx\n    https://nginx.org")), \
         patch.object(assist_guide.model_router, "chat", new=_capture_chat):
        res = await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=True, node_key="T3",
        )
    # The useful searxng result is cited; the empty milvus result is dropped.
    assert res["guidance_meta"]["research_sources"] == [
        {"query": "nginx install ubuntu", "kind": "searxng"}
    ]
    assert "## Research (confirmed" in captured["user"]
    assert "apt install nginx" in captured["user"]


@pytest.mark.asyncio
async def test_research_prepass_failsoft_on_tool_call_error():
    # tool_call fails → zero queries → guidance still generated, no sources.
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_tool_resp([], success=False))), \
         patch("app.modules.execution_agent._milvus_search", new=AsyncMock()) as mv, \
         patch("app.modules.execution_agent._searxng_search", new=AsyncMock()) as sx, \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("guided anyway"))):
        res = await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=True, node_key="T3",
        )
    assert res["status"] == "ready"
    assert res["guidance_meta"]["research_sources"] == []
    mv.assert_not_called()  # no queries → no confirm calls
    sx.assert_not_called()


def test_is_useful_grounding_filters_empty_and_failures():
    assert assist_guide._is_useful_grounding("[1] real result")
    assert not assist_guide._is_useful_grounding("")
    assert not assist_guide._is_useful_grounding("No search results found.")
    assert not assist_guide._is_useful_grounding("No knowledge base results found.")
    assert not assist_guide._is_useful_grounding("SearXNG search failed: timeout")
    assert not assist_guide._is_useful_grounding("Knowledge base search failed: boom")


# ── persistence + cache ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_persist_guidance_writes_and_commits():
    db = _db()
    await assist_guide.persist_guidance(
        session_id="s", node_key="T3", guidance="walk",
        guidance_meta={"status": "ready"}, status="ready", db=db,
    )
    db.execute.assert_awaited_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_read_cached_guidance_returns_none_when_not_ready():
    db = AsyncMock()
    row = {"guidance": "x", "guidance_meta": {}, "guidance_status": "failed",
           "guidance_generated_at": None}
    result = MagicMock()
    result.mappings.return_value.first.return_value = row
    db.execute = AsyncMock(return_value=result)
    assert await assist_guide.read_cached_guidance(session_id="s", node_key="T3", db=db) is None


@pytest.mark.asyncio
async def test_ensure_guidance_cache_hit_skips_llm():
    db = AsyncMock()
    with patch.object(assist_guide, "read_cached_guidance",
                      new=AsyncMock(return_value={"guidance": "cached", "status": "ready", "cached": True})), \
         patch.object(assist_guide.model_router, "chat", new=AsyncMock()) as chat, \
         patch.object(assist_guide, "persist_guidance", new=AsyncMock()) as persist:
        res = await assist_guide.ensure_guidance(
            session_id="s", node_key="T3", ctx=_ctx("shell"),
            research=False, force=False, db=db,
        )
    assert res["cached"] is True
    chat.assert_not_called()
    persist.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_guidance_force_regenerates_and_persists():
    db = AsyncMock()
    with patch.object(assist_guide, "read_cached_guidance", new=AsyncMock()) as cache, \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("fresh"))), \
         patch.object(assist_guide, "persist_guidance", new=AsyncMock()) as persist:
        res = await assist_guide.ensure_guidance(
            session_id="s", node_key="T3", ctx=_ctx("shell"),
            research=False, force=True, db=db,
        )
    cache.assert_not_called()  # force bypasses the cache read
    persist.assert_awaited_once()
    assert res["status"] == "ready"
    assert res["cached"] is False


# ── explicit one-off research ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_research_one_returns_sources_and_answer():
    with patch("app.modules.execution_agent._milvus_search",
               new=AsyncMock(return_value="No knowledge base results found.")), \
         patch("app.modules.execution_agent._searxng_search",
               new=AsyncMock(return_value="[1] answer body")), \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("Synthesized [1]"))):
        res = await assist_guide.research_one(question="what flag?")
    assert res["question"] == "what flag?"
    assert len(res["sources"]) == 1
    assert res["sources"][0]["kind"] == "searxng"
    assert res["answer"] == "Synthesized [1]"


@pytest.mark.asyncio
async def test_research_one_no_sources_no_synthesis():
    with patch("app.modules.execution_agent._milvus_search",
               new=AsyncMock(return_value="No knowledge base results found.")), \
         patch("app.modules.execution_agent._searxng_search",
               new=AsyncMock(return_value="No search results found.")), \
         patch.object(assist_guide.model_router, "chat", new=AsyncMock()) as chat:
        res = await assist_guide.research_one(question="obscure thing")
    assert res["sources"] == []
    assert res["answer"] is None
    chat.assert_not_called()  # nothing to synthesize from


# ── §17.487: environment block ─────────────────────────────────────────────


def test_render_environment_block_empty():
    assert assist_guide.render_environment_block(None) == ""
    assert assist_guide.render_environment_block({"profile": "", "substitutions": {}}) == ""


def test_render_environment_block_profile_and_subs():
    out = assist_guide.render_environment_block(
        {"profile": "Ubuntu 24.04, apt, bash", "substitutions": {"HOST_IP": "10.0.0.5"}}
    )
    assert "Operator environment" in out
    assert "Ubuntu 24.04" in out
    assert "HOST_IP = 10.0.0.5" in out


@pytest.mark.asyncio
async def test_guidance_injects_environment():
    captured = {}

    async def _capture_chat(messages, **kw):
        captured["user"] = messages[1]["content"]
        return _resp("walk")

    with patch.object(assist_guide.model_router, "chat", new=_capture_chat):
        await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=False, node_key="T3",
            environment={"profile": "Ubuntu 24.04", "substitutions": {"HOST_IP": "10.0.0.5"}},
        )
    assert "Operator environment" in captured["user"]
    assert "HOST_IP = 10.0.0.5" in captured["user"]


# ── §17.487: success verification ──────────────────────────────────────────


def _verdict_resp(outcome, reason="r", suggestion="s", success=True):
    r = MagicMock()
    r.success = success
    if success:
        call = MagicMock()
        call.arguments = {"outcome": outcome, "reason": reason, "suggestion": suggestion}
        r.tool_calls = [call]
    else:
        r.tool_calls = []
    return r


@pytest.mark.asyncio
async def test_verify_step_success_succeeded():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_verdict_resp("succeeded"))):
        v = await assist_guide.verify_step_success(
            title="t", task_prompt="p", tool="shell", evidence="ok, done",
        )
    assert v["outcome"] == "succeeded"


@pytest.mark.asyncio
async def test_verify_step_success_failed_carries_reason():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_verdict_resp("failed", reason="ModuleNotFoundError", suggestion="pip install x"))):
        v = await assist_guide.verify_step_success(
            title="t", task_prompt="p", tool="shell", evidence="Traceback ...",
        )
    assert v["outcome"] == "failed"
    assert "ModuleNotFoundError" in v["reason"]
    assert v["suggestion"] == "pip install x"


@pytest.mark.asyncio
async def test_verify_step_success_failsoft_on_no_tool_call():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_verdict_resp("failed", success=False))):
        v = await assist_guide.verify_step_success(
            title="t", task_prompt="p", tool="shell", evidence="x",
        )
    assert v["outcome"] == "unclear"
    assert v["reason"] == "verification unavailable"


@pytest.mark.asyncio
async def test_verify_step_success_failsoft_on_exception():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        v = await assist_guide.verify_step_success(
            title="t", task_prompt="p", tool="shell", evidence="x",
        )
    assert v["outcome"] == "unclear"


@pytest.mark.asyncio
async def test_verify_step_success_invalid_outcome_coerced_to_unclear():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_verdict_resp("maybe"))):
        v = await assist_guide.verify_step_success(
            title="t", task_prompt="p", tool="shell", evidence="x",
        )
    assert v["outcome"] == "unclear"


# ── §17.487: generate_fix ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_generate_fix_ready():
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("## Diagnosis\nmissing pkg\n## Fix\napt install x"))):
        res = await assist_guide.generate_fix(
            ctx=_ctx("shell"), error_text="command not found: x",
            research=False, node_key="T3",
        )
    assert res["status"] == "ready"
    assert "## Fix" in res["fix"]


@pytest.mark.asyncio
async def test_generate_fix_includes_error_and_env_in_prompt():
    captured = {}

    async def _capture_chat(messages, **kw):
        captured["user"] = messages[1]["content"]
        return _resp("fixed steps")

    with patch.object(assist_guide.model_router, "chat", new=_capture_chat):
        await assist_guide.generate_fix(
            ctx=_ctx("shell"), error_text="permission denied",
            research=False, node_key="T3",
            environment={"profile": "Ubuntu", "substitutions": {}},
        )
    assert "permission denied" in captured["user"]
    assert "Error the operator hit" in captured["user"]
    assert "Operator environment" in captured["user"]


@pytest.mark.asyncio
async def test_generate_fix_failsoft_empty():
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp(""))):
        res = await assist_guide.generate_fix(
            ctx=_ctx("LLM"), error_text="x", research=False, node_key="T3",
        )
    assert res["status"] == "failed"
    assert res["fix"] == ""


# ── §17.490: auto-learn substitutions ──────────────────────────────────────


def test_find_placeholders_distinct_and_skips_single_char():
    text = "ssh root@<HOST_IP>; set <HOST_IP>; cp <SRC_PATH> /x; junk <a>"
    assert assist_guide.find_placeholders(text) == ["HOST_IP", "SRC_PATH"]


def _values_resp(values, success=True):
    r = MagicMock()
    r.success = success
    if success:
        call = MagicMock()
        call.arguments = {"values": values}
        r.tool_calls = [call]
    else:
        r.tool_calls = []
    return r


@pytest.mark.asyncio
async def test_extract_substitutions_no_placeholders_skips_llm():
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()) as tc:
        out = await assist_guide.extract_substitutions(
            guidance_text="no slots here, just text", evidence="HOST_IP=10.0.0.5",
        )
    assert out == {}
    tc.assert_not_called()


@pytest.mark.asyncio
async def test_extract_substitutions_fills_from_evidence():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_values_resp(
                          {"HOST_IP": "10.0.0.5", "SRC_PATH": "/etc/app"}))):
        out = await assist_guide.extract_substitutions(
            guidance_text="ssh root@<HOST_IP>; cp <SRC_PATH> .",
            evidence="connected to 10.0.0.5, copied /etc/app",
        )
    assert out == {"HOST_IP": "10.0.0.5", "SRC_PATH": "/etc/app"}


@pytest.mark.asyncio
async def test_extract_substitutions_filters_unknown_keys_and_empty_and_brackets():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_values_resp(
                          {"<HOST_IP>": "10.0.0.5", "NOPE": "x", "SRC_PATH": "  "}))):
        out = await assist_guide.extract_substitutions(
            guidance_text="<HOST_IP> and <SRC_PATH>", evidence="...",
        )
    # bracket stripped → HOST_IP kept; NOPE not a placeholder → dropped;
    # SRC_PATH empty value → dropped.
    assert out == {"HOST_IP": "10.0.0.5"}


@pytest.mark.asyncio
async def test_extract_substitutions_failsoft():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_values_resp({}, success=False))):
        out = await assist_guide.extract_substitutions(
            guidance_text="<HOST_IP>", evidence="x",
        )
    assert out == {}
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("boom"))):
        out2 = await assist_guide.extract_substitutions(
            guidance_text="<HOST_IP>", evidence="x",
        )
    assert out2 == {}


# ── §17.491: sandbox-grounded codegen verification ─────────────────────────


async def test_verify_codegen_sandbox_fail_overrides_and_skips_llm(monkeypatch):
    monkeypatch.setattr(assist_guide.settings, "codegen_execution_check_enabled", True)
    monkeypatch.setattr(assist_guide.settings, "coderunner_url", "http://x")
    with patch.object(assist_guide, "_sandbox_codegen_check",
                      new=AsyncMock(return_value={"verdict": "fail", "reason": "NameError: foo"})), \
         patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()) as tc:
        v = await assist_guide.verify_step_success(
            title="t", task_prompt="p", tool="codegen", evidence="print(foo)",
        )
    assert v["outcome"] == "failed"
    assert v["grounded_by"] == "sandbox"
    assert "NameError" in v["reason"]
    tc.assert_not_called()  # a definite runtime error short-circuits the LLM


async def test_verify_codegen_sandbox_pass_falls_through_to_llm(monkeypatch):
    monkeypatch.setattr(assist_guide.settings, "codegen_execution_check_enabled", True)
    monkeypatch.setattr(assist_guide.settings, "coderunner_url", "http://x")
    with patch.object(assist_guide, "_sandbox_codegen_check",
                      new=AsyncMock(return_value={"verdict": "pass", "reason": "ran cleanly"})), \
         patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_verdict_resp("succeeded", reason="matches the task"))):
        v = await assist_guide.verify_step_success(
            title="t", task_prompt="p", tool="codegen", evidence="print(1)",
        )
    assert v["outcome"] == "succeeded"          # LLM judged task-fit
    assert v["grounded_by"] == "sandbox+model"  # and it actually ran
    assert "sandbox" in v["reason"].lower()


async def test_verify_codegen_sandbox_skip_uses_llm(monkeypatch):
    monkeypatch.setattr(assist_guide.settings, "codegen_execution_check_enabled", True)
    monkeypatch.setattr(assist_guide.settings, "coderunner_url", "http://x")
    with patch.object(assist_guide, "_sandbox_codegen_check",
                      new=AsyncMock(return_value={"verdict": "skip", "reason": "no python block"})), \
         patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_verdict_resp("succeeded"))):
        v = await assist_guide.verify_step_success(
            title="t", task_prompt="p", tool="codegen", evidence="some prose",
        )
    assert v["outcome"] == "succeeded"
    assert v["grounded_by"] == "model"


async def test_verify_non_codegen_skips_sandbox(monkeypatch):
    monkeypatch.setattr(assist_guide.settings, "codegen_execution_check_enabled", True)
    monkeypatch.setattr(assist_guide.settings, "coderunner_url", "http://x")
    with patch.object(assist_guide, "_sandbox_codegen_check", new=AsyncMock()) as sb, \
         patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_verdict_resp("succeeded"))):
        v = await assist_guide.verify_step_success(
            title="t", task_prompt="p", tool="shell", evidence="active",
        )
    sb.assert_not_called()
    assert v["grounded_by"] == "model"


async def test_verify_codegen_sandbox_disabled_skips(monkeypatch):
    monkeypatch.setattr(assist_guide.settings, "codegen_execution_check_enabled", False)
    with patch.object(assist_guide, "_sandbox_codegen_check", new=AsyncMock()) as sb, \
         patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_verdict_resp("succeeded"))):
        await assist_guide.verify_step_success(
            title="t", task_prompt="p", tool="codegen", evidence="print(1)",
        )
    sb.assert_not_called()  # gated off → no sandbox call


# ── §17.492: destructive-command safety gate ────────────────────────────────


def test_scan_destructive_flags_high_confidence():
    text = (
        "## Run this\n"
        "```bash\n"
        "$ rm -rf /var/lib/old\n"
        "sudo dd if=/dev/zero of=/dev/sda bs=4M\n"
        "mkfs.ext4 /dev/sdb1\n"
        "git push --force origin main\n"
        "```\n"
        "Then in psql:\n"
        "```sql\nDROP TABLE users;\nDELETE FROM logs;\n```\n"
    )
    found = assist_guide.scan_destructive(text)
    whys = " ".join(f["why"] for f in found)
    assert any("rm -rf" in f["line"] for f in found)
    assert "raw disk write" in whys
    assert "format filesystem" in whys
    assert "force push" in whys
    assert "DROP/TRUNCATE" in whys
    assert "no WHERE" in whys


def test_scan_destructive_no_false_positive_on_prose():
    text = (
        "This step will perform the migration and address the schema.\n"
        "Use `git add` and commit your work; the form renders fine.\n"
        "Run `ls -la` and `cat README.md` to inspect.\n"
        "DELETE FROM logs WHERE id < 100;  -- bounded, has WHERE\n"
    )
    assert assist_guide.scan_destructive(text) == []


def test_scan_destructive_dedups_by_line():
    text = "rm -rf /tmp/x\nrm -rf /tmp/x\n"
    assert len(assist_guide.scan_destructive(text)) == 1


@pytest.mark.asyncio
async def test_generate_guidance_attaches_destructive(monkeypatch):
    monkeypatch.setattr(assist_guide.settings, "assist_destructive_scan", True)
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("## Run this\n```\nrm -rf /opt/old\n```"))):
        res = await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=False, node_key="T3",
        )
    dest = res["guidance_meta"]["destructive"]
    assert dest and "rm -rf" in dest[0]["line"]


@pytest.mark.asyncio
async def test_generate_guidance_destructive_scan_disabled(monkeypatch):
    monkeypatch.setattr(assist_guide.settings, "assist_destructive_scan", False)
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("rm -rf /opt/old"))):
        res = await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=False, node_key="T3",
        )
    assert res["guidance_meta"]["destructive"] == []


# ── §17.493: streaming generation ──────────────────────────────────────────


def _astream(chunks):
    async def _agen(*a, **k):
        for c in chunks:
            yield c
    return _agen


@pytest.mark.asyncio
async def test_generate_guidance_stream_streams_then_done_and_persists():
    db = AsyncMock()
    with patch("app.modules.assist_guide.read_cached_guidance", new=AsyncMock(return_value=None)), \
         patch.object(assist_guide.model_router, "stream_chat", new=_astream(["## Run\n", "1. go"])), \
         patch.object(assist_guide, "persist_guidance", new=AsyncMock()) as persist:
        events = [ev async for ev in assist_guide.generate_guidance_stream(
            session_id="s", node_key="T3", ctx=_ctx("shell"), research=False, force=True, db=db)]
    deltas = [e["text"] for e in events if e["type"] == "delta"]
    done = [e for e in events if e["type"] == "done"][0]
    assert "".join(deltas) == "## Run\n1. go"
    assert done["status"] == "ready" and done["cached"] is False
    persist.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_guidance_stream_cache_hit_no_model_call():
    db = AsyncMock()
    with patch("app.modules.assist_guide.read_cached_guidance",
               new=AsyncMock(return_value={"guidance": "cached walk", "guidance_meta": {"x": 1}})), \
         patch.object(assist_guide.model_router, "stream_chat") as stream, \
         patch.object(assist_guide, "persist_guidance", new=AsyncMock()) as persist:
        events = [ev async for ev in assist_guide.generate_guidance_stream(
            session_id="s", node_key="T3", ctx=_ctx("shell"), research=False, force=False, db=db)]
    assert events[0] == {"type": "delta", "text": "cached walk"}
    assert events[-1]["type"] == "done" and events[-1]["cached"] is True
    stream.assert_not_called()   # cache hit → no model stream
    persist.assert_not_called()  # nothing new to persist


@pytest.mark.asyncio
async def test_generate_guidance_stream_empty_falls_back_to_nonstream():
    db = AsyncMock()
    empty = _astream([])  # stream yields nothing (thinking-model empty draw)
    with patch("app.modules.assist_guide.read_cached_guidance", new=AsyncMock(return_value=None)), \
         patch.object(assist_guide.model_router, "stream_chat", new=empty), \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("fallback body"))) as chat, \
         patch.object(assist_guide, "persist_guidance", new=AsyncMock()):
        events = [ev async for ev in assist_guide.generate_guidance_stream(
            session_id="s", node_key="T3", ctx=_ctx("shell"), research=False, force=True, db=db)]
    deltas = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "fallback body" in deltas
    chat.assert_awaited()  # §17.465 empty-guard preserved under streaming
    assert [e for e in events if e["type"] == "done"][0]["status"] == "ready"


@pytest.mark.asyncio
async def test_generate_guidance_stream_done_meta_has_destructive(monkeypatch):
    monkeypatch.setattr(assist_guide.settings, "assist_destructive_scan", True)
    db = AsyncMock()
    with patch("app.modules.assist_guide.read_cached_guidance", new=AsyncMock(return_value=None)), \
         patch.object(assist_guide.model_router, "stream_chat", new=_astream(["rm -rf /opt/old"])), \
         patch.object(assist_guide, "persist_guidance", new=AsyncMock()):
        events = [ev async for ev in assist_guide.generate_guidance_stream(
            session_id="s", node_key="T3", ctx=_ctx("shell"), research=False, force=True, db=db)]
    done = [e for e in events if e["type"] == "done"][0]
    assert done["guidance_meta"]["destructive"]
