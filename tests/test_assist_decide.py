"""§17.771 (Phase 1) — tests for the unified assist decision (`assist_decide`).

Covers the pure logic (deterministic signals, prompt assembly, fail-soft
shaping) and `decide_turn`'s validation of the model's tool call — ctx assembly,
the memory funnel and the model are mocked, so this asserts the decision
CONTRACT, not the LLM.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_decide
from app.modules.prompt_assembly import StepContext


# ── pure logic ────────────────────────────────────────────────────────────────

def test_signals_clean_shell_paste():
    s = assist_decide._compute_signals("root@pve:~# ls -la\ntotal 4", [])
    assert s == {"shell_paste": True, "shell_error": False, "last_assistant_was_fix": False}


def test_signals_shell_error():
    s = assist_decide._compute_signals("bash: zpool: command not found", [])
    assert s["shell_error"] is True


def test_signals_prose_is_not_a_paste():
    # an email-like user@host in prose must not read as a shell prompt line
    s = assist_decide._compute_signals("email me at bob@host.com about it", [])
    assert s["shell_paste"] is False


def test_signals_last_assistant_was_fix():
    hist = [
        {"role": "user", "content": "it broke"},
        {"role": "assistant", "content": "## 🔧 Troubleshooting\nrun this and tell me"},
        {"role": "user", "content": "root@pve:~# ip a"},
    ]
    s = assist_decide._compute_signals("root@pve:~# ip a", hist)
    assert s["last_assistant_was_fix"] is True


def test_decide_system_includes_decision_hint_only_when_decision():
    plain = assist_decide._decide_system(is_decision=False)
    dec = assist_decide._decide_system(is_decision=True)
    assert "THIS STEP IS A DECISION" not in plain
    assert "THIS STEP IS A DECISION" in dec
    # routing distinctions are inherited from the classifier prompt (no drift)
    assert "record_decision exactly once" in plain
    assert "Call classify_turn exactly once." not in plain  # stripped


def test_fallback_decision_is_low_confidence():
    f = assist_decide._fallback_decision("whatever")
    assert f["confidence"] == "low"
    assert f["action"] == "question"
    assert f["unavailable"] is True


# ── decide_turn validation contract ───────────────────────────────────────────

def _ctx():
    return StepContext(
        node_key="T3", title="Install CUDA toolkit", tool="shell", domain="gpu",
        system_prompt="sys", base_prompt="Install the CUDA toolkit in the guest VM.",
        upstream_outputs={}, upstream_truncated_keys=[], grounding="",
        grounding_kind=None, assembled_prompt="ap",
    )


def _db_with_session(status="active"):
    """A db mock whose first SELECT returns a steppable session row."""
    row = {"id": "S1", "job_id": "J1", "status": status,
           "current_node_key": "T3", "metadata": {}, "notes": []}
    res = MagicMock()
    res.mappings.return_value.first.return_value = row
    db = MagicMock()
    db.execute = AsyncMock(return_value=res)
    return db


def _model_resp(args: dict):
    call = MagicMock()
    call.arguments = args
    resp = MagicMock()
    resp.success = True
    resp.tool_calls = [call]
    return resp


def _patch_ctx_and_memory():
    """Patch the assist_agent ctx assembly + memory funnel decide_turn imports."""
    node_row = {"node_type": "task"}
    mem = MagicMock(environment={}, operator_notes=[], job_digest="", conversation="")
    p_ctx = patch("app.modules.assist_agent._assemble_ctx_for_node",
                  new=AsyncMock(return_value=(node_row, _ctx())))
    p_kind = patch("app.modules.assist_agent._collect_step_kind", return_value=None)
    p_mem = patch("app.modules.assist_agent.assemble_generation_memory",
                  new=AsyncMock(return_value=mem))
    return p_ctx, p_kind, p_mem


@pytest.mark.asyncio
async def test_decide_turn_happy_path_shapes_decision():
    p_ctx, p_kind, p_mem = _patch_ctx_and_memory()
    args = {"action": "ask", "query": "how do I install CUDA",
            "plan_impact": "none", "confidence": "high", "rationale": "help request",
            "note_kind": "bogus"}
    with p_ctx, p_kind, p_mem, \
         patch("app.modules.assist_decide.model_router.tool_call",
               new=AsyncMock(return_value=_model_resp(args))):
        r = await assist_decide.decide_turn(
            session_id="S1", message="help me install CUDA", db=_db_with_session())
    assert r["action"] == "ask"
    assert r["query"] == "how do I install CUDA"
    assert r["confidence"] == "high"
    assert r["note_kind"] == "note"  # invalid kind clamped
    assert r["node_key"] == "T3"
    assert r["unavailable"] is False


@pytest.mark.asyncio
async def test_decide_turn_bad_action_falls_back_low_conf():
    p_ctx, p_kind, p_mem = _patch_ctx_and_memory()
    args = {"action": "teleport", "confidence": "high", "rationale": "nope"}
    with p_ctx, p_kind, p_mem, \
         patch("app.modules.assist_decide.model_router.tool_call",
               new=AsyncMock(return_value=_model_resp(args))):
        r = await assist_decide.decide_turn(
            session_id="S1", message="do a barrel roll", db=_db_with_session())
    assert r["action"] == "question"
    assert r["confidence"] == "low"      # unknown action → safety-net fallback
    assert r["node_key"] == "T3"         # but context still attached


@pytest.mark.asyncio
async def test_decide_turn_suggestion_coerced_and_dropped_when_empty():
    p_ctx, p_kind, p_mem = _patch_ctx_and_memory()
    args = {"action": "question", "confidence": "medium", "rationale": "r",
            "suggestion": {"leaning": "", "why": "no leaning given"}}
    with p_ctx, p_kind, p_mem, \
         patch("app.modules.assist_decide.model_router.tool_call",
               new=AsyncMock(return_value=_model_resp(args))):
        r = await assist_decide.decide_turn(
            session_id="S1", message="what does step 2 mean?", db=_db_with_session())
    assert r["suggestion"] is None  # empty leaning → dropped


@pytest.mark.asyncio
async def test_decide_turn_unsteppable_session_is_unavailable():
    r = await assist_decide.decide_turn(
        session_id="S1", message="hi", db=_db_with_session(status="completed"))
    assert r["unavailable"] is True
    assert r["rationale"] == "session_not_steppable"


@pytest.mark.asyncio
async def test_fire_shadow_is_noop_when_valve_off():
    from app.config import settings
    with patch.object(settings, "assist_shadow_decision_enabled", False), \
         patch("app.modules.assist_decide.asyncio.create_task") as mk:
        assist_decide.fire_shadow_decision(
            session_id="S1", message="x", node_key="T3", history=[],
            classifier_intent="question")
    mk.assert_not_called()


def test_decide_prompt_routes_completion_to_submit_not_advance():
    """§17.771 (live-verify fix) — a completion report must route to SUBMIT
    (records evidence + marks done + advances), NOT advance (which maps to
    assist_next and neither records nor retires the step, so it re-presents the
    same step — the re-present loop found in live verification)."""
    extra = assist_decide._DECIDE_EXTRA
    assert "with no error → submit" in extra
    assert "Do NOT use advance for a completion" in extra
    # the old buggy wording (completion → advance) must be gone
    assert "with no error) → advance" not in extra


@pytest.mark.asyncio
async def test_decide_retries_once_on_transient_no_tool_call():
    """§17.771 (post-verify) — a transient no-tool-call is retried once instead of
    dropping straight to the cascade."""
    p_ctx, p_kind, p_mem = _patch_ctx_and_memory()
    no_tools = MagicMock(); no_tools.success = True; no_tools.tool_calls = []
    good = _model_resp({"action": "ask", "confidence": "high", "rationale": "r",
                        "query": "q"})
    tc = AsyncMock(side_effect=[no_tools, good])
    with p_ctx, p_kind, p_mem, \
         patch("app.modules.assist_decide.model_router.tool_call", new=tc):
        r = await assist_decide.decide_turn(
            session_id="S1", message="help", db=_db_with_session())
    assert r["action"] == "ask" and r["unavailable"] is False
    assert tc.await_count == 2  # retried once, succeeded on the second


@pytest.mark.asyncio
async def test_decide_gives_up_after_two_no_tool_calls():
    p_ctx, p_kind, p_mem = _patch_ctx_and_memory()
    no_tools = MagicMock(); no_tools.success = True; no_tools.tool_calls = []
    tc = AsyncMock(side_effect=[no_tools, no_tools])
    with p_ctx, p_kind, p_mem, \
         patch("app.modules.assist_decide.model_router.tool_call", new=tc):
        r = await assist_decide.decide_turn(
            session_id="S1", message="x", db=_db_with_session())
    assert r["unavailable"] is True and r["rationale"] == "no_tool_call"
    assert tc.await_count == 2  # one retry, then cascade fallback
