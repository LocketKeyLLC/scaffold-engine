"""§17.1615 — a decision the operator made at a decision pause reaches every later step as a fact.

Live (job 0fb4ea7f, 2026-10-16): T10 recorded the two chosen models; T12, drafted after it, tested `qwen2.5-coder:14b`
(nobody chose it; `ollama run` would have downloaded it). `operator_decisions` read only decisions written into a
step's own text, never a decision node's record; and `plan_context` cut every fact at 220 characters.
"""
from __future__ import annotations

from app.modules import runbook_templates as rt
from app.modules.runbook_inputs import operator_decisions

T10 = {"node_key": "T10", "status": "done", "node_type": "decision", "completed_at": "2026-10-16 02:00:00+00",
       "output_text": ("Decision (operator): Cyber-Tiel-Coder-35B-A3B (coding) + Qwen3.8-27B Heretic (reasoning)\n"
                       "Note: Serve two models: hf.co/peculiar-ragdoll/Cyber-Tiel-Coder-35B-A3B-GGUF:UD-IQ4_XS and "
                       "hf.co/0bserverx/Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF:Q4_K_M.\n"
                       "Options considered: Qwen2.5-Coder-32B; DeepSeek-Coder-V2")}


def test_a_decision_node_s_record_becomes_a_fact_with_its_note():
    facts = operator_decisions([T10])
    assert len(facts) == 1 and facts[0].startswith("Operator decision (T10, 2026-10-16)")
    assert "Cyber-Tiel-Coder-35B-A3B-GGUF:UD-IQ4_XS" in facts[0] and "Options considered" not in facts[0]


def test_a_pending_or_non_decision_node_is_not_one():
    assert operator_decisions([{**T10, "status": "pending"}]) == []
    assert operator_decisions([{**T10, "node_type": "task"}]) == []


def test_the_drafter_gets_the_decision_whole():
    ctx = rt.plan_context("", {"facts": operator_decisions([T10]) + ["x" * 500]})
    assert "Qwen3.8-27B-Heretic-Abliterated-Uncensored-GGUF:Q4_K_M" in ctx
    assert "x" * 221 not in ctx, "other facts are still cut"
