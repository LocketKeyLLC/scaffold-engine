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
