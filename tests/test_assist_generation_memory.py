"""§17.751 — the single session-memory funnel.

``assemble_generation_memory`` is the ONE place a generation turn pulls its
session memory (environment + facts, operator notes, whole-project digest, recent
dialogue with a transcript fallback, and the running step recap). Every
operator-facing generation site routes through it so a new or edited site cannot
silently go memory-blind — the recurring failure the log closed one site at a
time (§17.650 / §17.687 / §17.720 / §17.726 / §17.738 / §17.745, each titled
"the LAST blind injection site").
"""
import inspect
from unittest.mock import AsyncMock

import pytest

from app.modules import assist_agent


@pytest.mark.asyncio
async def test_assemble_generation_memory_carries_env_and_notes(monkeypatch):
    from app.config import settings
    # Disable the DB/LLM-backed sources so the bundle is deterministic offline;
    # each already fails soft to "" so the caller can thread it unconditionally.
    monkeypatch.setattr(settings, "assist_job_context_enabled", False, raising=False)
    monkeypatch.setattr(settings, "assist_step_recap_enabled", False, raising=False)
    monkeypatch.setattr(settings, "assist_status_panel_enabled", False, raising=False)

    sess = {
        "job_id": "job-1",
        "metadata": {"environment": {"profile": "root@pve",
                                     "substitutions": {"HOST": "pve"}}},
        "notes": [{"kind": "constraint", "text": "only 2 NICs"}],
    }
    mem = await assist_agent.assemble_generation_memory(
        session_id="s1", nk="T3", sess=sess, db=AsyncMock(),
        exclude_tail="done", history=[{"role": "operator", "content": "hi"}],
        digest_excludes=set(),
    )
    assert isinstance(mem, assist_agent.GenerationMemory)
    # env + facts and operator notes always reach the bundle
    assert mem.environment.get("profile") == "root@pve"
    assert mem.operator_notes and mem.operator_notes[0]["text"] == "only 2 NICs"
    # gated-off sources degrade to empty strings — never None, never a raise
    assert mem.job_digest == ""
    assert mem.recap == ""
    assert isinstance(mem.conversation, str)
    # client-supplied history passes through (no transcript rebuild needed)
    assert mem.history == [{"role": "operator", "content": "hi"}]


def test_every_generation_site_routes_through_the_funnel():
    """A generation path that skips ``assemble_generation_memory`` is a
    memory-blind site waiting to happen. Enforce the funnel structurally so a new
    or refactored site can't quietly drop a source."""
    for fn_name in (
        "generate_step_guidance",
        "generate_step_guidance_stream",
        "run_step_fix",
        "run_step_research",
        "run_step_decision",
    ):
        src = inspect.getsource(getattr(assist_agent, fn_name))
        assert "assemble_generation_memory(" in src, (
            f"{fn_name} does not route through assemble_generation_memory — it "
            f"will silently go memory-blind on some session sources (§17.751)"
        )


# ── §17.844 — brief essentials reach the funnel ─────────────────────────────

class _Rows:
    """Minimal async-DB stub: .execute() returns .mappings().first() -> row."""
    def __init__(self, row):
        self._row = row

    async def execute(self, *_a, **_k):
        row = self._row
        class _R:
            def mappings(self):
                class _M:
                    def first(self_inner):
                        return row
                return _M()
        return _R()


@pytest.mark.asyncio
async def test_post_confirm_brief_prefers_research_data_copy():
    """The approval-gate answers live ONLY in research_data.brief — the stale
    jobs.refined_brief must lose when the post-confirm copy exists."""
    row = {
        "refined_brief": {"description": "old", "constraints": ["keep proxmox"]},
        "research_data": {"brief": {"description": "new", "user_feedback": "Q: x\nA: y"}},
    }
    brief = await assist_agent._post_confirm_brief(db=_Rows(row), job_id="j1")
    assert brief["description"] == "new"
    assert brief["user_feedback"] == "Q: x\nA: y"


@pytest.mark.asyncio
async def test_post_confirm_brief_falls_back_pre_confirm():
    row = {"refined_brief": {"description": "phase1"}, "research_data": None}
    brief = await assist_agent._post_confirm_brief(db=_Rows(row), job_id="j1")
    assert brief["description"] == "phase1"


def test_brief_essentials_block_carries_inventory_answers_constraints():
    block = assist_agent._brief_essentials_block({
        "description": "Build a homelab",
        "constraints": ["PRESERVE the existing Proxmox install"],
        "inputs_available": ["2x Xeon E5-2695 V2", "Tesla P40 24GB"],
        "user_feedback": "Q: hypervisor?\nA: Proxmox already installed",
    })
    assert "PRESERVE the existing Proxmox install" in block
    assert "Tesla P40 24GB" in block
    assert "Proxmox already installed" in block
    # the do-not-re-ask contract is explicit
    assert "re-ask" in block
    assert assist_agent._brief_essentials_block({}) == ""


@pytest.mark.asyncio
async def test_funnel_prepends_brief_essentials(monkeypatch):
    """job_digest leads with the operator-established facts even when digest
    and recap are empty (step 1 — previously the funnel carried NOTHING about
    the project at that point)."""
    from app.config import settings
    monkeypatch.setattr(settings, "assist_job_context_enabled", True, raising=False)
    monkeypatch.setattr(settings, "assist_step_recap_enabled", False, raising=False)

    async def fake_digest(**_k):
        return ""
    async def fake_recap(**_k):
        return ""
    async def fake_project_recap(**_k):
        return ""
    async def fake_post_brief(**_k):
        return {"inputs_available": ["HP V1910-24G switch"],
                "user_feedback": "Q: k8s?\nA: on the laptops"}
    monkeypatch.setattr(assist_agent, "_job_digest_for", fake_digest)
    monkeypatch.setattr(assist_agent, "get_step_recap", fake_recap)
    monkeypatch.setattr(assist_agent, "get_project_recap", fake_project_recap)
    monkeypatch.setattr(assist_agent, "_post_confirm_brief", fake_post_brief)

    sess = {"job_id": "job-1", "metadata": {}, "notes": []}
    mem = await assist_agent.assemble_generation_memory(
        session_id="s1", nk="T1", sess=sess, db=AsyncMock(),
        history=[], digest_excludes=set(),
    )
    assert "HP V1910-24G switch" in mem.job_digest
    assert "on the laptops" in mem.job_digest
    assert mem.job_digest.startswith("── PROJECT BRIEF")
