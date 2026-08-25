"""§17.753 — the cross-step "living project recap" (§17.679): a distilled, cached,
evolving whole-project state board, refreshed as steps complete, prepended to the
job digest in the §17.751 funnel and threaded into the note/pivot analyzer.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import assist_agent, assist_guide, assist_replan
from app.providers.base import ToolCall

_JID = "91a94870-f38c-48e3-877a-225766039969"


def _result(first_=None, all_=None):
    m = MagicMock()
    m.mappings.return_value.first.return_value = first_
    m.mappings.return_value.all.return_value = all_ if all_ is not None else []
    return m


# ── render / summarize ──────────────────────────────────────────────────────


def test_render_project_recap_block():
    assert assist_guide.render_project_recap_block(None) == ""
    out = assist_guide.render_project_recap_block("GOAL: build a homelab")
    assert "Whole-project state" in out and "GOAL: build a homelab" in out


@pytest.mark.asyncio
async def test_summarize_project_prompt_carries_nodes_and_ledgers():
    captured = {}

    async def _cun(*args, **kwargs):
        captured["messages"] = args[1] if len(args) > 1 else kwargs.get("messages")
        return types.SimpleNamespace(success=True, text="GOAL: x", model="m", error=None)

    with patch.object(assist_guide, "chat_until_nonempty", new=_cun):
        out = await assist_guide.summarize_project_progress(
            goal="build a homelab",
            nodes_block="- T1 (done): Install Proxmox — produced: installed 8.2",
            facts_block="Known facts about the operator's system (observed):\n- 2 NICs",
            notes_block="- (decision) use ZFS mirror",
        )
    assert out == "GOAL: x"
    user = captured["messages"][1]["content"]
    assert "build a homelab" in user
    assert "Install Proxmox" in user
    assert "2 NICs" in user and "use ZFS mirror" in user


# ── get_project_recap ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_project_recap_disabled_returns_empty(monkeypatch):
    monkeypatch.setattr(settings, "assist_project_recap_enabled", False, raising=False)
    out = await assist_agent.get_project_recap(job_id=_JID, db=AsyncMock())
    assert out == ""


@pytest.mark.asyncio
async def test_get_project_recap_uses_cache_when_not_grown(monkeypatch):
    monkeypatch.setattr(settings, "assist_project_recap_enabled", True, raising=False)
    monkeypatch.setattr(settings, "assist_project_recap_every", 1, raising=False)
    monkeypatch.setattr(settings, "assist_project_recap_min_nodes", 1, raising=False)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result(first_={"refined_brief": {"description": "g"},
                        "project_recap": "GOAL: cached", "project_recap_nodes": 2}),
        _result(all_=[{"node_key": "T1", "title": "a", "status": "done",
                       "output_text": "x", "execution_order": 1},
                      {"node_key": "T2", "title": "b", "status": "done",
                       "output_text": "y", "execution_order": 2}]),  # 2 done == watermark
    ])
    with patch.object(assist_guide, "summarize_project_progress",
                      new=AsyncMock()) as summ:
        out = await assist_agent.get_project_recap(job_id=_JID, db=db)
    assert out == "GOAL: cached"
    summ.assert_not_awaited()   # 2 done < watermark(2)+every(1)=3 → no refresh


@pytest.mark.asyncio
async def test_get_project_recap_refreshes_when_grown(monkeypatch):
    monkeypatch.setattr(settings, "assist_project_recap_enabled", True, raising=False)
    monkeypatch.setattr(settings, "assist_project_recap_every", 1, raising=False)
    monkeypatch.setattr(settings, "assist_project_recap_min_nodes", 1, raising=False)
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result(first_={"refined_brief": {"description": "build a homelab"},
                        "project_recap": "GOAL: stale", "project_recap_nodes": 1}),
        _result(all_=[
            {"node_key": "T1", "title": "Install Proxmox", "status": "done",
             "output_text": "installed 8.2", "execution_order": 1},
            {"node_key": "T2", "title": "Create VM", "status": "done",
             "output_text": "vm 100", "execution_order": 2},
            {"node_key": "T3", "title": "Configure net", "status": "pending",
             "output_text": None, "execution_order": 3},
        ]),
        _result(first_={"notes": [{"kind": "decision", "text": "use ZFS mirror"}],
                        "metadata": {"environment": {"facts": ["2 NICs"]}}}),  # session
    ])
    db.commit = AsyncMock()
    # §17.812 — the cache persist runs on its OWN session, never the caller's.
    cache_db = AsyncMock()
    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=cache_db)
    acm.__aexit__ = AsyncMock(return_value=False)
    with patch.object(assist_agent, "async_session", return_value=acm), \
         patch.object(assist_guide, "summarize_project_progress",
                      new=AsyncMock(return_value="GOAL: fresh board")) as summ:
        out = await assist_agent.get_project_recap(job_id=_JID, db=db)
    assert out == "GOAL: fresh board"
    summ.assert_awaited_once()
    kw = summ.await_args.kwargs
    assert kw["goal"] == "build a homelab"
    assert "Install Proxmox" in kw["nodes_block"] and "T3 (pending)" in kw["nodes_block"]
    assert "2 NICs" in kw["facts_block"] and "use ZFS mirror" in kw["notes_block"]
    # §17.812 — persisted with the new done-node watermark (2) on the cache
    # session; the caller's transaction untouched.
    upd = [c for c in cache_db.execute.await_args_list if "project_recap = :r" in str(c.args[0])]
    assert upd and upd[0].args[1]["n"] == 2
    cache_db.commit.assert_awaited_once()
    db.commit.assert_not_awaited()


# ── funnel prepends the project recap to the job digest ─────────────────────


@pytest.mark.asyncio
async def test_funnel_prepends_project_recap_to_digest():
    sess = {"job_id": _JID, "metadata": {}, "notes": []}
    with patch.object(assist_agent, "get_project_recap",
                      new=AsyncMock(return_value="GOAL: the arc")), \
         patch.object(assist_agent, "_job_digest_for",
                      new=AsyncMock(return_value="## RAW step outputs")), \
         patch.object(assist_agent, "get_step_recap", new=AsyncMock(return_value="")), \
         patch.object(assist_agent, "_history_or_transcript",
                      new=AsyncMock(return_value=[])):
        mem = await assist_agent.assemble_generation_memory(
            session_id="s1", nk="T3", sess=sess, db=AsyncMock(), digest_excludes=set(),
        )
    assert mem.project_recap == "GOAL: the arc"
    # the distilled recap leads the project-context block, raw outputs follow
    assert "Whole-project state" in mem.job_digest
    assert "GOAL: the arc" in mem.job_digest
    assert "## RAW step outputs" in mem.job_digest
    assert mem.job_digest.index("GOAL: the arc") < mem.job_digest.index("RAW step outputs")


# ── note-impact analyzer grounds in the project recap ───────────────────────


@pytest.mark.asyncio
async def test_note_impact_prompt_carries_project_recap():
    rows = [{"node_key": "T5", "title": "Deploy SaaS billing", "description": None}]
    db = AsyncMock()
    db.execute = AsyncMock(side_effect=[
        _result(all_=rows),                                    # pending nodes
        _result(first_={"refined_brief": {"goals": ["g1"]}}),  # brief
    ])
    resp = types.SimpleNamespace(
        text="", success=True, error=None,
        tool_calls=[ToolCall(id="t0", name="record_plan_impact", arguments={"affected": []})],
    )
    with patch("app.model_router.tool_call", AsyncMock(return_value=resp)) as tc:
        await assist_replan.analyze_note_impact(
            db=db, job_id=_JID, note_text="forget SaaS, make it e-commerce",
            note_kind="decision",
            project_recap_block=assist_guide.render_project_recap_block(
                "GOAL: SaaS billing platform\nDECISIONS: Stripe billing chosen"),
        )
    prompt = tc.await_args.kwargs["messages"][0]["content"]
    assert "Whole-project state" in prompt
    assert "Stripe billing chosen" in prompt
