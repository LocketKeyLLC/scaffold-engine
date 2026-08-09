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


# ── §17.640: always-on beginner-audience framing ────────────────────────────


def test_every_guide_prompt_carries_beginner_framing():
    """§17.640 — the walkthrough must never assume prior expertise. The
    beginner-audience floor is baked into EVERY human guide/fix system prompt
    (shell/codegen/non-code/fix), so it holds regardless of verbosity."""
    for tool in ("shell", "codegen", "LLM", None):
        s = assist_guide.guide_system_for_tool(tool)
        assert "assume no prior knowledge" in s.lower(), tool
        # the specific failure that prompted this: connecting two machines
        assert "connecting one machine to another" in s
    assert "assume no prior knowledge" in assist_guide.GUIDE_SYSTEM_FIX.lower()


def test_every_guide_prompt_carries_simplicity_floor():
    """§17.673 — the walkthrough must serve the MOST unknowledgeable person: the
    plain-language + confirm-as-you-go floor is baked into every guide/fix prompt
    (via _AUDIENCE_FRAMING). Simple words, exact button text, and a short 'what
    you should see' check after actions that show feedback — without re-bloating
    (§17.643): the confirmations are explicitly flagged as checks, not padding."""
    for tool in ("shell", "codegen", "LLM", None):
        s = assist_guide.guide_system_for_tool(tool).lower()
        assert "plain language" in s, tool
        assert "simplest everyday words" in s, tool
        assert "should see" in s, tool
        # the confirmations must NOT reopen the verbosity hole §17.643 closed
        assert "not background" in s or "never excuse padding" in s, tool
    assert "plain language" in assist_guide.GUIDE_SYSTEM_FIX.lower()


def test_every_guide_prompt_carries_pacing_floor():
    """§17.641/§17.643 — a step's walkthrough must be paced (phased +
    checkpoints) AND brief. The pacing/brevity floor is in every generation
    prompt: chunk the necessary actions, but favor the fewest words a beginner
    needs to act. The pre-§17.643 anti-brevity phrasing ('do NOT cut it') is
    gone — it produced ~870-word single-step walkthroughs."""
    for tool in ("shell", "codegen", "LLM", None):
        s = assist_guide.guide_system_for_tool(tool)
        low = s.lower()
        assert "pacing" in low, tool
        assert "checkpoint" in low, tool
        assert "phases" in low, tool
        # §17.643 — brevity is now part of the floor; the old anti-brevity
        # instruction must NOT reappear (it fought the "too long" fix).
        assert "fewest words" in low, tool
        assert "do not cut it" not in low, tool
        # §17.674 — a length ceiling + single-golden-path (no inline decision
        # trees) so a "little too long" step trims instead of sprawling.
        assert "150-300 words" in low, tool
        assert "one common path" in low, tool
        assert "decision tree" in low, tool


def test_research_synth_prompt_is_actionable_not_a_source_recap():
    """§17.674 — the pivot `ask`/research answer must synthesize how to ACHIEVE
    the goal from the sources (forum/doc/search) adapted to the project, NOT recap
    what a page says. The live failure: it relayed a forum thread instead of the
    steps the operator takes."""
    low = assist_guide._RESEARCH_SYNTH_SYSTEM.lower()
    # sources are raw material, not the answer
    assert "raw material" in low
    assert "mine" in low
    # explicit ban on the forum-recap failure mode
    assert "the forum suggests" in low or "according to the thread" in low
    assert "recap" in low
    # actionable + beginner-simple
    assert "steps the operator should take" in low or "what the operator actually does" in low
    assert "copy-paste" in low


def test_fix_prompt_carries_root_cause_rule():
    """§17.734 — the fix prompt must handle a broken FOUNDATION (a prerequisite
    the plan assumed is set up but isn't) as the real problem, not band-aid it
    and push the operator toward completing the nominal step. The live failure:
    'install NVIDIA driver' needed internet, but the VM's bridge was isolated —
    the fix kept steering back to the driver instead of addressing networking."""
    fix = assist_guide.GUIDE_SYSTEM_FIX
    low = fix.lower()
    assert "root-cause rule" in low
    # names the broken-foundation / contradicted-belief case
    assert "was never actually set up" in low or "isn't actually working" in low \
        or "assumed was already set up" in low
    # must NOT rush forward past it
    assert "do not rush" in low or "not a quick hurdle" in low \
        or "until the foundation is confirmed" in low
    # can escalate to a real plan step instead of a fragile inline workaround
    assert "needs its own step" in low
    # the pre-§17.734 over-scoping that caused the push-forward is gone
    assert "for this error only. nothing more" not in low


def test_every_guide_prompt_carries_target_safety():
    """§17.648 — every human guide/fix prompt must carry the target-machine
    safety rule: a wipe/install step acts ON the target (in place, booted from
    media), never by attaching the target's drives to the operator's own laptop,
    and never runs a destructive command against the machine the operator is at.
    The live failure: a Proxmox-host 'Wipe storage devices' step told the user to
    pull the server's drives, plug them into their laptop, and dd them there."""
    for tool in ("shell", "codegen", "LLM", None):
        s = assist_guide.guide_system_for_tool(tool).lower()
        assert "target-machine safety" in s, tool
        assert "attach them to their own" in s or "relocate its hardware" in s, tool
        assert "never run a destructive command" in s, tool
    fix = assist_guide.GUIDE_SYSTEM_FIX.lower()
    assert "target-machine safety" in fix
    # The pre-§17.648 misleading absolute is gone from the shell/runbook framing.
    assert "perform this step themselves on their own machine." not in \
        assist_guide.guide_system_for_tool("shell").lower()


def test_beginner_framing_survives_every_verbosity():
    """The floor holds at every verbosity — terse must NOT reintroduce an
    expert assumption (the pre-§17.640 terse said 'assume an expert operator')."""
    base = assist_guide.guide_system_for_tool("LLM")
    for v in (None, "normal", "terse", "detailed"):
        s = assist_guide.apply_verbosity(base, v)
        assert "assume no prior knowledge" in s.lower(), v
    terse = assist_guide.apply_verbosity(base, "terse")
    assert "assume an expert" not in terse.lower()


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


def test_render_research_block_is_correctness_only():
    """§17.643 — the research block must instruct the model to use the facts for
    ACCURACY only, not to reproduce their depth. The pre-§17.643 header
    ('authoritative facts; use them') led the model to transcribe the research
    into the walkthrough, ~doubling its length (471→871 words for one step)."""
    block = assist_guide._render_research_block(
        [{"query": "q", "kind": "searxng", "text": "apt install nginx"}]
    )
    low = block.lower()
    assert "for your accuracy only" in low
    assert "not to reproduce" in low
    assert "the reader needs the steps, not the research" in low
    # empty sources still no-op
    assert assist_guide._render_research_block([]) == ""


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
    # §17.500 — research_one is DEEP: mock the page-fetch helper (not the
    # snippet path) so the test stays hermetic.
    with patch("app.modules.execution_agent._milvus_search",
               new=AsyncMock(return_value="No knowledge base results found.")), \
         patch.object(assist_guide, "_deep_web_sources",
                      new=AsyncMock(return_value=[
                          {"query": "what flag?", "kind": "web",
                           "text": "the --flag enables it", "url": "https://docs/x"}])), \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("Synthesized [1]"))):
        res = await assist_guide.research_one(question="what flag?")
    assert res["question"] == "what flag?"
    assert len(res["sources"]) == 1
    assert res["sources"][0]["kind"] == "web"
    assert res["sources"][0]["url"] == "https://docs/x"
    assert res["answer"] == "Synthesized [1]"


@pytest.mark.asyncio
async def test_research_one_no_sources_no_synthesis():
    # deep fetch finds nothing AND the snippet fallback is empty → 0 sources.
    with patch("app.modules.execution_agent._milvus_search",
               new=AsyncMock(return_value="No knowledge base results found.")), \
         patch.object(assist_guide, "_deep_web_sources", new=AsyncMock(return_value=[])), \
         patch("app.modules.execution_agent._searxng_search",
               new=AsyncMock(return_value="No search results found.")), \
         patch.object(assist_guide.model_router, "chat", new=AsyncMock()) as chat:
        res = await assist_guide.research_one(question="obscure thing")
    assert res["sources"] == []
    assert res["answer"] is None
    chat.assert_not_called()  # nothing to synthesize from


@pytest.mark.asyncio
async def test_research_one_synthesizes_from_job_context_without_web_sources():
    # §17.650 — a question answerable purely from the project's own prior work
    # must still synthesize an answer even when the open web returns nothing,
    # and the project context must be folded into the synthesis prompt.
    with patch("app.modules.execution_agent._milvus_search",
               new=AsyncMock(return_value="No knowledge base results found.")), \
         patch.object(assist_guide, "_deep_web_sources", new=AsyncMock(return_value=[])), \
         patch("app.modules.execution_agent._searxng_search",
               new=AsyncMock(return_value="No search results found.")), \
         patch.object(assist_guide, "_focus_web_query",
                      new=AsyncMock(return_value="connect two computers")), \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("Use HOST_A=10.0.0.1"))) as chat:
        res = await assist_guide.research_one(
            question="how do I connect the two computers?",
            job_context="## Project context — done work\nHOST_A=10.0.0.1, HOST_B=10.0.0.2 via crossover",
        )
    assert res["sources"] == []           # web/KB were dry
    assert res["answer"] == "Use HOST_A=10.0.0.1"  # but the project context carried it
    chat.assert_called_once()
    # chat_until_nonempty forwards messages= as a kwarg to model_router.chat.
    messages = chat.call_args.kwargs["messages"]
    user_msg = next(m["content"] for m in messages if m["role"] == "user")
    assert "HOST_A=10.0.0.1" in user_msg   # project state reached the model


@pytest.mark.asyncio
async def test_research_one_context_hint_biases_kb_only():
    # §17.650 — context_hint augments the LOCAL-KB embedding query but NOT the
    # web query (open-web results must not be polluted with project entities).
    milvus = AsyncMock(return_value="No knowledge base results found.")
    with patch("app.modules.execution_agent._milvus_search", new=milvus), \
         patch.object(assist_guide, "_deep_web_sources", new=AsyncMock(return_value=[])), \
         patch("app.modules.execution_agent._searxng_search",
               new=AsyncMock(return_value="No search results found.")), \
         patch.object(assist_guide.model_router, "chat", new=AsyncMock(return_value=_resp("x"))):
        await assist_guide.research_one(
            question="what subnet?", context_hint="HomeLab HOST_A HOST_B",
        )
    kb_query = milvus.call_args[0][0]
    assert "what subnet?" in kb_query
    assert "HOST_A" in kb_query  # entity hint folded into the KB query


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


def test_render_session_memory_consolidates_all():
    # §17.710b — one block with context + facts + provided + notes + grounding.
    out = assist_guide.render_session_memory(
        {"profile": "root@pve web console",
         "facts": ["Existing Proxmox VE 9.2.6 (not fresh)"],
         "substitutions": {"HOST_IP": "10.0.0.5"}},
        [{"kind": "constraint", "text": "only 2 physical NICs"}],
    )
    assert "Session memory" in out
    assert "do NOT assume a fresh" in out          # grounding rule baked in
    assert "root@pve web console" in out
    assert "Existing Proxmox VE 9.2.6" in out
    assert "HOST_IP = 10.0.0.5" in out
    assert "only 2 physical NICs" in out


def test_render_session_memory_reset_supersedes_stale_facts():
    # §17.714 — when the operator declares a fresh start, the append-only facts
    # ledger + the "never assume fresh" rule must NOT override the decision.
    # Regression for the live homelab report: operator abandoned in-place
    # reconfig for a fresh Proxmox install, but guidance kept operating on the
    # stale CPU-governor facts because they led the block as authoritative.
    out = assist_guide.render_session_memory(
        {"profile": "root@pve single shell",
         "facts": ["Current governor: performance", "Scaling driver: intel_cpufreq"],
         "substitutions": {}},
        [{"kind": "decision",
          "text": "Operator has decided to abandon in-place reconfiguration and "
                  "instead perform a fresh Proxmox VE install."}],
    )
    assert out.startswith("## Session memory — the operator has CHANGED DIRECTION")
    assert "do NOT assume a fresh" not in out          # anti-fresh rule suspended
    assert "SUSPENDED" in out
    # the decision leads; the now-stale facts are demoted below it
    assert out.index("abandon in-place") < out.index("Current governor")
    assert "Earlier observations" in out
    assert "re-verify" in out


def test_render_session_memory_benign_note_keeps_normal_grounding():
    # §17.714 — a plain constraint/preference must NOT trip reset mode; the
    # normal anti-fresh grounding stays in force (no false positive).
    out = assist_guide.render_session_memory(
        {"profile": "root@pve", "facts": ["Existing Proxmox VE 9.2.6 (not fresh)"],
         "substitutions": {}},
        [{"kind": "constraint", "text": "only 2 physical NICs; prefer a clean minimal config"}],
    )
    assert "do NOT assume a fresh" in out               # normal grounding intact
    assert "CHANGED DIRECTION" not in out


def test_operator_reset_intent_media_install_language():
    # §17.720 — the live pivot said none of the §17.714 phrases. The operator
    # announced an OS install from removable media / a new ISO / an in-progress
    # installer, and every pattern missed — so answers kept arguing them back to
    # the in-place plan. These are the exact reported phrasings.
    for txt in (
        "Operator has decided to set up the new Proxmox ISO first before other tasks.",
        "no, i am currently installing it, and have three options",
        "This is the options from the USB they are to install one of those options",
        "I booted from the USB installer",
        "installing Proxmox from the flash drive now",
    ):
        assert assist_guide._operator_reset_intent(
            [{"kind": "decision", "text": txt}]), txt


def test_operator_reset_intent_plain_install_not_tripped():
    # A bare "install <software>" (the whole project installs things) and other
    # benign phrasings must NOT flip reset mode.
    for txt in (
        "install nginx on the VM",
        "please install docker and configure the compose file",
        "download the ISO for later",
        "only 2 physical NICs; prefer a clean minimal config",
        "now installing the jellyfin package via apt",
        "currently installing docker on the media VM",
    ):
        assert not assist_guide._operator_reset_intent(
            [{"kind": "note", "text": txt}]), txt


def test_render_session_memory_empty():
    assert assist_guide.render_session_memory({"profile": "", "substitutions": {}, "facts": []}) == ""
    assert assist_guide.render_session_memory(None, None) == ""


def test_render_session_memory_budget_keeps_facts_drops_notes():
    # Over budget → notes/provided dropped before facts (grounding-critical).
    mem = assist_guide.render_session_memory(
        {"profile": "ctx", "facts": ["FACT-KEEP-ME"], "substitutions": {}},
        [{"kind": "note", "text": "LOW-PRIORITY-NOTE " * 40}],  # big → over budget
        budget=300,
    )
    assert "FACT-KEEP-ME" in mem            # facts survive
    assert "LOW-PRIORITY-NOTE" not in mem   # notes dropped first


def test_render_session_memory_big_ledger_trims_facts_not_notes():
    # §17.722 — the live regression: a mature facts ledger alone exceeded the
    # budget and the old section-pop evicted notes → values → the ENTIRE facts
    # section, leaving only header + profile. Now the facts LIST trims
    # (oldest dropped, newest kept) and the small high-signal sections survive.
    facts = [f"FACT-{i:03d} " + "x" * 90 for i in range(40)]  # ~3.8k chars
    mem = assist_guide.render_session_memory(
        {"profile": "root@pve single shell", "facts": facts,
         "substitutions": {"VMID": "100", "VM_NAME": "AI-VM"}},
        [{"kind": "decision", "text": "bridge must be named DeFruscioBridge"}],
        budget=3000,
    )
    assert len(mem) <= 3000
    assert "DeFruscioBridge" in mem          # notes survive
    assert "VMID = 100" in mem               # provided values survive
    assert "root@pve single shell" in mem    # profile survives
    assert "FACT-039" in mem                 # newest fact kept
    assert "FACT-000" not in mem             # oldest fact trimmed
    assert "older facts omitted" in mem      # trim is announced, not silent


def test_render_session_memory_under_budget_keeps_everything():
    facts = [f"FACT-{i}" for i in range(5)]
    mem = assist_guide.render_session_memory(
        {"profile": "ctx", "facts": facts, "substitutions": {"K": "V"}},
        [{"kind": "note", "text": "NOTE-1"}],
        budget=4000,
    )
    for f in facts:
        assert f in mem
    assert "NOTE-1" in mem and "K = V" in mem
    assert "older facts omitted" not in mem


def test_render_session_memory_reset_mode_trims_facts_keeps_direction():
    # §17.714 + §17.722 — reset mode: the operator's direction leads and stays;
    # the demoted earlier-observations list trims newest-kept under budget.
    facts = [f"OLD-FACT-{i:03d} " + "y" * 90 for i in range(40)]
    mem = assist_guide.render_session_memory(
        {"profile": "root@pve", "facts": facts, "substitutions": {}},
        [{"kind": "decision", "text": "wipe it and do a clean install of Proxmox"}],
        budget=3000,
    )
    assert len(mem) <= 3000
    assert "CHANGED DIRECTION" in mem
    assert "clean install of Proxmox" in mem  # direction survives
    assert "OLD-FACT-039" in mem              # newest demoted fact kept
    assert "OLD-FACT-000" not in mem          # oldest trimmed


def test_memory_or_legacy_valve_off_uses_legacy(monkeypatch):
    monkeypatch.setattr(assist_guide.settings, "assist_unified_memory_enabled", False)
    parts = assist_guide._render_memory_or_legacy(
        {"profile": "P", "facts": ["F"], "substitutions": {}}, [{"kind": "note", "text": "N"}])
    joined = "\n".join(parts)
    assert "Operator environment" in joined          # legacy env block
    assert "Operator notes & additions" in joined     # legacy notes block
    assert "Session memory" not in joined


def test_memory_or_legacy_valve_on_uses_unified(monkeypatch):
    monkeypatch.setattr(assist_guide.settings, "assist_unified_memory_enabled", True)
    monkeypatch.setattr(assist_guide.settings, "assist_umem_inject", True)
    parts = assist_guide._render_memory_or_legacy(
        {"profile": "P", "facts": ["F"], "substitutions": {}}, [{"kind": "note", "text": "N"}])
    joined = "\n".join(parts)
    assert "Session memory" in joined
    assert "Operator environment" not in joined       # legacy renderers not used
    assert "Operator notes & additions" not in joined


def test_guide_prompt_uses_unified_memory_when_valve_on(monkeypatch):
    monkeypatch.setattr(assist_guide.settings, "assist_unified_memory_enabled", True)
    monkeypatch.setattr(assist_guide.settings, "assist_umem_inject", True)
    prompt = assist_guide._build_guide_user_prompt(
        _ctx("shell"), None, [], None,
        environment={"profile": "root@pve", "facts": ["Existing PVE 9.2.6"], "substitutions": {}},
        operator_notes=[{"kind": "constraint", "text": "keep the existing VMs"}],
    )
    assert "Session memory" in prompt
    assert "Existing PVE 9.2.6" in prompt
    assert "keep the existing VMs" in prompt


def test_render_environment_block_includes_facts():
    # §17.709 — the facts ledger renders with an explicit don't-assume-fresh rule.
    out = assist_guide.render_environment_block(
        {"profile": "", "substitutions": {},
         "facts": ["Existing Proxmox VE 9.2.6 (not a fresh install)",
                   "Network: vmbr0 = 192.168.1.156/24"]}
    )
    assert "Known facts about the operator's system" in out
    assert "not a fresh install" in out
    assert "do NOT assume a fresh" in out          # grounding rule present
    # facts alone (no profile/subs) are enough to render the block
    assert out != ""


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
    # §17.710b — valve-agnostic: assert the environment VALUES are injected
    # (they appear in both the legacy env block and the unified memory block),
    # not the header, so the test doesn't depend on the assist_umem_inject state.
    assert "Ubuntu 24.04" in captured["user"]
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


@pytest.mark.asyncio
async def test_verify_step_success_incomplete_is_a_valid_outcome():
    # §17.731 — the live failure: "Install guest OS" evidence is only the ISO
    # download + boot menu → 'incomplete' (setup done, deliverable not).
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_verdict_resp(
                          "incomplete",
                          reason="Evidence downloads the installer and shows the boot menu; the OS is not installed.",
                          suggestion="Run the installer, create a user, and boot into the installed system."))):
        v = await assist_guide.verify_step_success(
            title="Install guest OS", task_prompt="Install Ubuntu Server in VM 100",
            tool="shell",
            evidence="wget ubuntu-22.04.3-live-server.iso ... GNU GRUB ... Try or Install Ubuntu Server",
        )
    assert v["outcome"] == "incomplete"
    assert "not installed" in v["reason"]
    assert v["suggestion"]


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
async def test_generate_fix_threads_job_digest_into_prompt():
    # §17.653 — troubleshooting is project-aware: the whole-project digest is
    # folded into the fix prompt just like the guidance path.
    captured = {}

    async def _capture_chat(messages, **kw):
        captured["user"] = messages[1]["content"]
        return _resp("fixed steps")

    with patch.object(assist_guide.model_router, "chat", new=_capture_chat):
        await assist_guide.generate_fix(
            ctx=_ctx("shell"), error_text="permission denied",
            research=False, node_key="T3",
            job_digest="## Project context — done work\nZFS pool tank on the Supermicro host",
        )
    assert "ZFS pool tank on the Supermicro host" in captured["user"]


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


def _facts_resp(facts, success=True):
    r = MagicMock()
    r.success = success
    r.text = ""
    if success:
        call = MagicMock()
        call.arguments = {"facts": facts}
        r.tool_calls = [call]
    else:
        r.tool_calls = []
    return r


@pytest.mark.asyncio
async def test_distill_facts_parses_and_bounds():
    # §17.709 — parses the facts array; blanks dropped, each fact length-bounded.
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_facts_resp(
                          ["Existing Proxmox VE 9.2.6", "x" * 400, "   "]))):
        out = await assist_guide.distill_facts(
            evidence="root@pve:~# pveversion\npve-manager/9.2.6",
        )
    facts = out["facts"]
    assert "Existing Proxmox VE 9.2.6" in facts
    assert all(f.strip() for f in facts)          # blanks dropped
    assert all(len(f) <= 300 for f in facts)      # bounded
    assert out["superseded"] == []


def _grounding_resp(contradicts, reason="", success=True):
    r = MagicMock()
    r.success = success
    r.text = ""
    if success:
        call = MagicMock()
        call.arguments = {"contradicts": contradicts, "reason": reason}
        r.tool_calls = [call]
    else:
        r.tool_calls = []
    return r


@pytest.mark.asyncio
async def test_check_grounding_flags_contradiction():
    env = {"profile": "", "substitutions": {},
           "facts": ["Existing Proxmox VE 9.2.6 (not a fresh install)"]}
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_grounding_resp(
                          True, "assumes a fresh install but host is existing PVE 9.2.6"))):
        out = await assist_guide.check_grounding(
            evidence="Assumption: fresh Proxmox VE server — no stale accounts.", environment=env)
    assert out["contradicts"] is True
    assert "fresh" in out["reason"]


@pytest.mark.asyncio
async def test_check_grounding_noop_without_memory():
    # No facts/profile/subs/notes → no memory to check → no LLM call.
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()) as tc:
        out = await assist_guide.check_grounding(
            evidence="anything", environment={"profile": "", "substitutions": {}, "facts": []})
    assert out["contradicts"] is False
    tc.assert_not_called()


@pytest.mark.asyncio
async def test_check_grounding_failsoft():
    env = {"facts": ["Existing PVE"], "substitutions": {}, "profile": ""}
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("model down"))):
        out = await assist_guide.check_grounding(evidence="x", environment=env)
    assert out["contradicts"] is False


@pytest.mark.asyncio
async def test_distill_facts_empty_evidence_skips_llm():
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()) as tc:
        out = await assist_guide.distill_facts(evidence="   ")
    assert out == {"facts": [], "superseded": []}
    tc.assert_not_called()


@pytest.mark.asyncio
async def test_distill_facts_failsoft():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("model down"))):
        out = await assist_guide.distill_facts(evidence="some output")
    assert out == {"facts": [], "superseded": []}


# ── §17.715 — unconditional per-turn derive ────────────────────────────────


def _turn_memory_resp(notes, facts, success=True, superseded=None):
    r = MagicMock()
    r.success = success
    r.text = ""
    call = MagicMock()
    call.arguments = {"notes": notes, "facts": facts}
    if superseded is not None:
        call.arguments["superseded_facts"] = superseded
    r.tool_calls = [call] if success else []
    return r


@pytest.mark.asyncio
async def test_distill_turn_memory_shapes_and_filters():
    # §17.715 — valid notes/facts pass; bad kinds + blanks are dropped.
    resp = _turn_memory_resp(
        notes=[
            {"kind": "decision", "text": "Operator will drop the VPN and use WireGuard."},
            {"kind": "bogus", "text": "should be filtered (bad kind)"},
            {"kind": "constraint", "text": "  "},  # blank → dropped
        ],
        facts=["Router is a UniFi UDM at 192.168.1.1", "  "],
    )
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock(return_value=resp)):
        out = await assist_guide.distill_turn_memory(message="let's drop the VPN, use WireGuard")
    assert [n["kind"] for n in out["notes"]] == ["decision"]
    assert "WireGuard" in out["notes"][0]["text"]
    assert out["facts"] == ["Router is a UniFi UDM at 192.168.1.1"]


@pytest.mark.asyncio
async def test_distill_turn_memory_empty_message_skips_llm():
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()) as tc:
        out = await assist_guide.distill_turn_memory(message="   ")
    assert out == {"notes": [], "facts": [], "superseded": []}
    tc.assert_not_called()


@pytest.mark.asyncio
async def test_distill_turn_memory_failsoft():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("model down"))):
        out = await assist_guide.distill_turn_memory(message="anything substantive here")
    assert out == {"notes": [], "facts": [], "superseded": []}


@pytest.mark.asyncio
async def test_distill_turn_memory_superseded_filtered_to_known():
    # §17.725 — only verbatim (normalized) matches against known_facts survive;
    # hallucinated retractions are dropped, and the LEDGER's spelling returns.
    known = ["P40 GPU is in IOMMU group 13", "ZFS pool 'oasis' active"]
    resp = _turn_memory_resp(
        notes=[], facts=["Tesla P40 (02:00.0) is in IOMMU group 37"],
        superseded=["p40 gpu is in iommu group 13",      # case-normalized match
                    "some fact that was never recorded"],  # hallucinated → dropped
    )
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock(return_value=resp)):
        out = await assist_guide.distill_turn_memory(
            message="the P40 is actually 02:00.0 in group 37",
            known_facts=known,
        )
    assert out["superseded"] == ["P40 GPU is in IOMMU group 13"]  # ledger spelling
    assert out["facts"] == ["Tesla P40 (02:00.0) is in IOMMU group 37"]


def test_match_superseded_caps_at_five():
    known = [f"FACT-{i}" for i in range(10)]
    out = assist_guide._match_superseded([f"fact-{i}" for i in range(10)], known)
    assert len(out) == 5


def test_match_superseded_empty_without_known():
    assert assist_guide._match_superseded(["anything"], None) == []
    assert assist_guide._match_superseded(None, ["known"]) == []


@pytest.mark.asyncio
async def test_distill_facts_superseded_filtered_to_known():
    # §17.725 — the submit-path distiller returns retractions the same way.
    r = MagicMock()
    r.success = True
    r.text = ""
    call = MagicMock()
    call.arguments = {
        "facts": ["Tesla P40 (02:00.0) is in IOMMU group 37"],
        "superseded_facts": ["P40 GPU is in IOMMU group 13", "never recorded"],
    }
    r.tool_calls = [call]
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock(return_value=r)):
        out = await assist_guide.distill_facts(
            evidence="lspci shows 02:00.0 Tesla P40",
            known_facts=["P40 GPU is in IOMMU group 13"],
        )
    assert out["superseded"] == ["P40 GPU is in IOMMU group 13"]
    assert out["facts"] == ["Tesla P40 (02:00.0) is in IOMMU group 37"]


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


def test_scan_destructive_rm_requires_actual_flag():
    """§17.613 (audit #7) — the rm detector must anchor on a real -r/-f/-R flag.
    Ordinary rm and the interactive-SAFE `rm -i` must NOT trip the gate (crying
    wolf blunts it for the genuine `rm -rf` case); the destructive forms must."""
    # Non-destructive rm forms — no r/f/R flag → must NOT fire.
    for safe in ("rm config.conf", "rm myfile", "rm -i file", "rm ./notes.md"):
        assert assist_guide.scan_destructive(safe) == [], f"false positive on: {safe!r}"
    # Genuinely destructive forms — must fire.
    for danger in ("rm -rf /tmp/x", "rm -fr build", "rm -r dir", "rm -f a.txt", "rm -R dir"):
        assert assist_guide.scan_destructive(danger), f"missed: {danger!r}"


def test_scan_destructive_dedups_by_line():
    text = "rm -rf /tmp/x\nrm -rf /tmp/x\n"
    assert len(assist_guide.scan_destructive(text)) == 1


def test_scan_destructive_command_verbs_anchor_to_command_start():
    """§17.644 — a destructive tool named in PROSE or a numbered heading must NOT
    fire; only an actual command (tool at the start, after any sudo/env prefix)
    does. The live browser test flagged 7 lines on one T1 step, most of them
    prose containing the word 'parted'."""
    # Prose / headings that merely MENTION the tool — must NOT fire.
    for prose in (
        "### Phase 2 – Create a single partition with parted",
        "1. Open parted on the disk:",
        "4. Exit parted:",
        "Use fdisk or parted to inspect the layout.",
        "This will format the drive with mkfs later.",
        "Be careful with dd — it writes raw bytes.",
    ):
        assert assist_guide.scan_destructive(prose) == [], f"false positive on: {prose!r}"
    # Real commands (optionally sudo/env-prefixed) — must fire.
    for danger in (
        "sudo parted /dev/sdb",
        "parted /dev/<DISK_DEVICE>",
        "mkfs.ext4 /dev/sdb1",
        "sudo dd if=/dev/zero of=/dev/sda",
        "FOO=bar dd if=/dev/zero of=/dev/sdb",
        "kubectl delete pod x",
    ):
        assert assist_guide.scan_destructive(danger), f"missed: {danger!r}"


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


# ── §17.499 — verbosity / skill-level control ───────────────────────────────


def test_apply_verbosity_terse_and_detailed_and_normal():
    assert "TERSE" in assist_guide.apply_verbosity("SYS", "terse")
    assert "DETAILED" in assist_guide.apply_verbosity("SYS", "detailed")
    assert assist_guide.apply_verbosity("SYS", "normal") == "SYS"   # no change
    assert assist_guide.apply_verbosity("SYS", None) == "SYS"
    assert assist_guide.apply_verbosity("SYS", "bogus") == "SYS"    # unknown → no change


@pytest.mark.asyncio
async def test_generate_guidance_threads_verbosity_into_system():
    captured = {}

    async def _capture_chat(messages, **kw):
        captured["system"] = messages[0]["content"]
        return _resp("walk")

    with patch.object(assist_guide.model_router, "chat", new=_capture_chat):
        await assist_guide.generate_guidance(
            ctx=_ctx("shell"), research=False, node_key="T3", verbosity="terse",
        )
    assert "TERSE" in captured["system"]


@pytest.mark.asyncio
async def test_generate_fix_threads_verbosity_into_system():
    captured = {}

    async def _capture_chat(messages, **kw):
        captured["system"] = messages[0]["content"]
        return _resp("## Fix\nx")

    with patch.object(assist_guide.model_router, "chat", new=_capture_chat):
        await assist_guide.generate_fix(
            ctx=_ctx("shell"), error_text="boom", research=False,
            node_key="T3", verbosity="detailed",
        )
    assert "DETAILED" in captured["system"]


# ── §17.500 — deep research (page fetch + extract) ──────────────────────────


@pytest.mark.asyncio
async def test_deep_web_sources_fetches_and_extracts(monkeypatch):
    monkeypatch.setattr(assist_guide.settings, "assist_research_fetch_top_n", 2)
    with patch.object(assist_guide, "_searxng_structured",
                      new=AsyncMock(return_value=[
                          {"title": "t", "content": "snip", "url": "https://a"},
                          {"title": "t2", "content": "snip2", "url": "https://b"}])), \
         patch("app.modules.research_agent._fetch_and_extract",
               new=AsyncMock(return_value=[{"url": "https://a", "content": "FULL PAGE BODY"}])):
        out = await assist_guide._deep_web_sources("q", top_n=2)
    assert out == [{"query": "q", "kind": "web", "text": "FULL PAGE BODY", "url": "https://a"}]


@pytest.mark.asyncio
async def test_deep_web_sources_empty_on_no_results(monkeypatch):
    with patch.object(assist_guide, "_searxng_structured", new=AsyncMock(return_value=[])):
        assert await assist_guide._deep_web_sources("q", top_n=2) == []


@pytest.mark.asyncio
async def test_confirm_query_deep_uses_pages_then_skips_snippet(monkeypatch):
    monkeypatch.setattr(assist_guide.settings, "assist_research_fetch_top_n", 2)
    with patch("app.modules.execution_agent._milvus_search",
               new=AsyncMock(return_value="No knowledge base results found.")), \
         patch.object(assist_guide, "_deep_web_sources",
                      new=AsyncMock(return_value=[{"query": "q", "kind": "web",
                                                   "text": "page", "url": "https://a"}])), \
         patch("app.modules.execution_agent._searxng_search",
               new=AsyncMock(return_value="[1] snippet")) as snip:
        out = await assist_guide._confirm_query("q", node_key="T1", domain=None, deep=True)
    assert [s["kind"] for s in out] == ["web"]   # deep page used
    snip.assert_not_called()                       # snippet path skipped when pages found


@pytest.mark.asyncio
async def test_confirm_query_deep_falls_back_to_snippet_when_no_pages(monkeypatch):
    monkeypatch.setattr(assist_guide.settings, "assist_research_fetch_top_n", 2)
    with patch("app.modules.execution_agent._milvus_search",
               new=AsyncMock(return_value="No knowledge base results found.")), \
         patch.object(assist_guide, "_deep_web_sources", new=AsyncMock(return_value=[])), \
         patch("app.modules.execution_agent._searxng_search",
               new=AsyncMock(return_value="[1] snippet body")):
        out = await assist_guide._confirm_query("q", node_key="T1", domain=None, deep=True)
    assert [s["kind"] for s in out] == ["searxng"]  # fell back to snippet


@pytest.mark.asyncio
async def test_confirm_query_shallow_never_fetches(monkeypatch):
    with patch("app.modules.execution_agent._milvus_search",
               new=AsyncMock(return_value="No knowledge base results found.")), \
         patch.object(assist_guide, "_deep_web_sources", new=AsyncMock()) as deep, \
         patch("app.modules.execution_agent._searxng_search",
               new=AsyncMock(return_value="[1] snippet")):
        out = await assist_guide._confirm_query("q", node_key="T1", domain=None, deep=False)
    deep.assert_not_called()  # auto-guide pre-pass stays snippet-fast
    assert [s["kind"] for s in out] == ["searxng"]


# ── §17.727 — ledger consolidation (merge redundant same-truth facts) ───────


def _merge_resp(merges, success=True):
    r = MagicMock()
    r.success = success
    r.text = ""
    call = MagicMock()
    call.arguments = {"merges": merges}
    r.tool_calls = [call] if success else []
    return r


@pytest.mark.asyncio
async def test_consolidate_facts_validates_and_resolves_to_texts():
    facts = ["oasis is a ZFS pool", "Storage 'oasis' (zfspool) active",
             "oasis has 5.7 TB free", "Next available VM ID is 100"]
    merges = [
        {"replaces": [0, 1, 2], "text": "Storage 'oasis' is an active ZFS pool with 5.7 TB free"},
        {"replaces": [3], "text": "a rewrite of one fact"},          # <2 → dropped
        {"replaces": [0, 99], "text": "overlap + out-of-range"},     # 0 used, 99 invalid → dropped
    ]
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_merge_resp(merges))):
        out = await assist_guide.consolidate_facts(facts)
    assert len(out) == 1
    assert out[0]["replaces"] == facts[:3]      # indices resolved to ledger texts
    assert "5.7 TB" in out[0]["text"]


@pytest.mark.asyncio
async def test_consolidate_facts_short_ledger_skips_llm():
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock()) as tc:
        assert await assist_guide.consolidate_facts(["only one"]) == []
    tc.assert_not_called()


@pytest.mark.asyncio
async def test_consolidate_facts_failsoft():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("model down"))):
        assert await assist_guide.consolidate_facts(["a", "b"]) == []


@pytest.mark.asyncio
async def test_consolidate_facts_skips_overlong_merge_instead_of_truncating():
    # §17.727 — a replacement too long to keep whole would lose details if
    # truncated; the group is skipped (originals kept) — lossless over tidy.
    facts = ["detail A", "detail B"]
    merges = [{"replaces": [0, 1], "text": "x" * 700}]
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(return_value=_merge_resp(merges))):
        out = await assist_guide.consolidate_facts(facts)
    assert out == []


# ── §17.729 — keyword-focus the ask query + deep-path relevance filter ───────


@pytest.mark.asyncio
async def test_focus_web_query_compresses_conversational_question():
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("Proxmox attach ISO qm set boot order"))):
        out = await assist_guide._focus_web_query(
            "can you walk me through fixing the VM 100 step by step",
            role="model_general",
        )
    assert out == "Proxmox attach ISO qm set boot order"


@pytest.mark.asyncio
async def test_focus_web_query_skips_llm_for_short_query():
    with patch.object(assist_guide.model_router, "chat", new=AsyncMock()) as chat:
        out = await assist_guide._focus_web_query("what flag enables it", role="model_general")
    assert out == "what flag enables it"
    chat.assert_not_called()   # ≤6 words → already keyword-ish


@pytest.mark.asyncio
async def test_focus_web_query_failsoft_returns_original():
    long_q = "can you please walk me through the entire thing step by step now"
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(side_effect=RuntimeError("model down"))):
        out = await assist_guide._focus_web_query(long_q, role="model_general")
    assert out == long_q


@pytest.mark.asyncio
async def test_research_one_searches_focused_query_not_raw_question():
    # §17.729 — the retrieval query is the focused one; the raw question still
    # drives synthesis.
    captured = {}

    async def _fake_confirm(q, **kw):
        captured["kb_q"] = q                    # positional = raw question (KB)
        captured["web_q"] = kw.get("web_query")  # §17.729 focused web query
        return [{"query": q, "kind": "web", "text": "docs", "url": "https://d"}]

    with patch.object(assist_guide, "_focus_web_query",
                      new=AsyncMock(return_value="ubuntu server latest lts iso")), \
         patch.object(assist_guide, "_confirm_query", new=_fake_confirm), \
         patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp("answer"))) as chat:
        res = await assist_guide.research_one(
            question="hey can you help me get the newest ubuntu onto this vm please")
    assert captured["web_q"] == "ubuntu server latest lts iso"   # web uses focused
    assert "newest ubuntu" in captured["kb_q"]                   # KB keeps raw (§17.650)
    user_msg = next(m["content"] for m in chat.call_args.kwargs["messages"]
                    if m["role"] == "user")
    assert "newest ubuntu" in user_msg   # original question still framed synthesis


@pytest.mark.asyncio
async def test_searxng_structured_uses_backbone_and_filters_junk():
    # §17.729 — deep path: curated engines (not categories=general) + relevance
    # filter drops keyword-matcher junk before the fetcher sees it.
    calls = {}

    def _fake_get(path, params=None):
        calls["params"] = params
        r = MagicMock(status_code=200)
        r.raise_for_status = MagicMock()
        r.json = MagicMock(return_value={"results": [
            {"title": "Ubuntu Server download page", "content": "get ubuntu lts",
             "url": "https://ubuntu.com/download"},
            {"title": "Current | Future of Banking", "content": "crypto",
             "url": "https://bank.example"},
        ]})
        return r

    client = MagicMock()
    client.get = AsyncMock(side_effect=_fake_get)
    with patch("app.utils.http_clients.get_searxng_client", return_value=client):
        out = await assist_guide._searxng_structured("ubuntu server download")
    assert "categories" not in calls["params"]         # backbone, not categories=general
    assert "engines" in calls["params"]
    urls = [r["url"] for r in out]
    assert "https://ubuntu.com/download" in urls
    assert "https://bank.example" not in urls          # junk filtered


# ── §17.736 — draft an inserted step ────────────────────────────────────────


@pytest.mark.asyncio
async def test_draft_step_parses_title_and_description():
    r = MagicMock()
    r.success = True
    r.text = ""
    call = MagicMock()
    call.arguments = {"title": "Configure the VM's network for internet access",
                      "description": "Give VM 100 internet; done when it can ping 8.8.8.8."}
    r.tool_calls = [call]
    with patch.object(assist_guide.model_router, "tool_call", new=AsyncMock(return_value=r)):
        out = await assist_guide.draft_step(
            request="set up the VM networking properly",
            job_context="## Project goal\nBuild a homelab on Proxmox")
    assert "network" in out["title"].lower()
    assert "ping" in out["description"].lower()


@pytest.mark.asyncio
async def test_draft_step_failsoft_uses_request():
    with patch.object(assist_guide.model_router, "tool_call",
                      new=AsyncMock(side_effect=RuntimeError("model down"))):
        out = await assist_guide.draft_step(request="set up the VM networking")
    assert out["title"] == "set up the VM networking"     # falls back to the request
    assert out["description"]


@pytest.mark.asyncio
async def test_draft_step_empty_request():
    out = await assist_guide.draft_step(request="   ")
    assert out["title"] and out["description"]            # never empty


# ── §17.738 — per-step progress recap ───────────────────────────────────────


@pytest.mark.asyncio
async def test_summarize_step_progress_returns_recap():
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(return_value=_resp(
                          "GOAL: VM internet\nDONE: NAT set up\nOPEN: console blank\nCONTEXT: run on HOST"))):
        out = await assist_guide.summarize_step_progress(
            title="Configure networking",
            transcript="Operator: apt fails\nAssistant: set up NAT\nOperator: done")
    assert "GOAL:" in out and "DONE:" in out


@pytest.mark.asyncio
async def test_summarize_step_progress_empty_transcript_skips_llm():
    with patch.object(assist_guide.model_router, "chat", new=AsyncMock()) as chat:
        out = await assist_guide.summarize_step_progress(title="x", transcript="   ")
    assert out == ""
    chat.assert_not_called()


@pytest.mark.asyncio
async def test_summarize_step_progress_failsoft():
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(side_effect=RuntimeError("model down"))):
        out = await assist_guide.summarize_step_progress(title="x", transcript="something")
    assert out == ""


def test_render_step_recap_block():
    assert assist_guide.render_step_recap_block("") == ""
    assert assist_guide.render_step_recap_block(None) == ""
    b = assist_guide.render_step_recap_block("GOAL: x\nOPEN: y")
    assert "Where we are on this step" in b
    assert "GOAL: x" in b


# ── §17.741 operator-facing status panel + "do this next" callout ──────────


def test_parse_recap_labeled_fields():
    recap = (
        "GOAL: VM online so apt works\n"
        "DONE: bridge vmbr0 created\n"
        "- VM boots\n"
        "OPEN: no internet uplink yet\n"
        "NEXT: add the uplink NIC to vmbr0, then re-test apt\n"
        "CONTEXT: host=pve, VM=100"
    )
    p = assist_guide.parse_recap(recap)
    assert p["goal"] == "VM online so apt works"
    assert p["done"] == ["bridge vmbr0 created", "VM boots"]
    assert p["open"] == ["no internet uplink yet"]
    assert p["next"].startswith("add the uplink NIC")
    assert p["context"] == "host=pve, VM=100"


def test_parse_recap_tolerates_markdown_and_empty():
    assert assist_guide.parse_recap("") == {
        "goal": "", "done": [], "open": [], "constraints": [], "next": "", "context": ""
    }
    assert assist_guide.parse_recap(None)["done"] == []
    # bold/heading/bullet prefixes on the label line still parse
    p = assist_guide.parse_recap("**GOAL:** x\n## DONE\n* a\n* b")
    assert p["goal"] == "x"
    assert p["done"] == ["a", "b"]


def test_render_status_panel_shows_progress():
    recap = "GOAL: g\nDONE: a\nOPEN: b\nNEXT: c"
    out = assist_guide.render_status_panel(recap)
    assert "📍 Where we are on this step" in out
    assert "**Goal:** g" in out
    assert "✅ **Done:** a" in out
    assert "⬜ **Still open:** b" in out
    assert "👉 **Next:** c" in out


def test_render_status_panel_multiple_done_joined():
    out = assist_guide.render_status_panel("GOAL: g\nDONE: a\n- x\n- y\nNEXT: z")
    assert "✅ **Done:** a · x · y" in out


def test_render_status_panel_empty_without_real_progress():
    # goal-only (turn 1, nothing done yet) → no panel, not noise
    assert assist_guide.render_status_panel("GOAL: g") == ""
    assert assist_guide.render_status_panel("") == ""
    assert assist_guide.render_status_panel(None) == ""


def test_apply_next_callout_appends_when_enabled():
    base = "SYSTEM PROMPT BODY"
    out = assist_guide.apply_next_callout(base, is_decision=False, enabled=True)
    assert out.startswith(base)
    assert "## 👉 Do this next" in out
    assert len(out) > len(base)


def test_apply_next_callout_noop_when_disabled_or_decision():
    base = "SYSTEM PROMPT BODY"
    assert assist_guide.apply_next_callout(base, is_decision=False, enabled=False) == base
    # a decision node never gets a "do this now" callout (it's a choice)
    assert assist_guide.apply_next_callout(base, is_decision=True, enabled=True) == base


def test_step_recap_system_prompt_carries_next_label():
    # the recap now distills a NEXT action for the panel + model grounding
    assert "NEXT:" in assist_guide._STEP_RECAP_SYSTEM


# ── §17.742 problem-solving discipline + recap CONSTRAINTS ─────────────────


def test_parse_recap_captures_constraints():
    recap = (
        "GOAL: reset the ai-vm password\n"
        "DONE: tried guestmount, virt-customize — both failed\n"
        "CONSTRAINTS: no copy-paste in noVNC\n"
        "- guest agent not running\n"
        "NEXT: edit the GRUB boot entry to init=/bin/bash"
    )
    p = assist_guide.parse_recap(recap)
    assert p["constraints"] == ["no copy-paste in noVNC", "guest agent not running"]
    # CONSTRAINTS must not bleed into CONTEXT (shared 'CON' prefix)
    assert p["context"] == ""
    assert p["next"].startswith("edit the GRUB")


def test_render_status_panel_shows_constraints():
    recap = "GOAL: g\nOPEN: b\nCONSTRAINTS: no copy-paste\n- offline only\nNEXT: c"
    out = assist_guide.render_status_panel(recap)
    assert "⚠️ **Constraints:** no copy-paste · offline only" in out


def test_render_status_panel_shows_when_only_constraints():
    # constraints alone are worth surfacing even before other progress
    out = assist_guide.render_status_panel("GOAL: g\nCONSTRAINTS: no copy-paste")
    assert "⚠️ **Constraints:** no copy-paste" in out
    assert out != ""


def test_apply_problem_solving_appends_when_enabled():
    base = "SYSTEM BODY"
    out = assist_guide.apply_problem_solving(base, enabled=True)
    assert out.startswith(base)
    low = out.lower()
    assert "honor the confirmed constraints" in low
    assert "stop cycling" in low
    assert "match the operator" in low


def test_apply_problem_solving_noop_when_disabled():
    base = "SYSTEM BODY"
    assert assist_guide.apply_problem_solving(base, enabled=False) == base


def test_step_recap_system_prompt_carries_constraints_label():
    assert "CONSTRAINTS:" in assist_guide._STEP_RECAP_SYSTEM
