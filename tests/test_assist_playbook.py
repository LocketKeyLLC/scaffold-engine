"""§17.881 — commit-time reconciliation + session playbook + fix escalation."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_memory, assist_render
from app.modules.assist_agent import _fix_failure_streak

pytestmark = pytest.mark.asyncio


# ── playbook merge (set_environment) ─────────────────────────────────────


async def test_set_environment_merges_playbook_deduped():
    from app.modules.assist_environment import set_environment
    db = AsyncMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {"metadata": {
        "environment": {"playbook": {"proven": ["EXISTING entry from an earlier step"]}}}}
    db.execute = AsyncMock(return_value=row)
    env = await set_environment(
        session_id="s",
        playbook_proven=["tarball via servarr.com works"],
        playbook_ruled_out=["apt.servarr.com repo — unreachable from LXC"],
        db=db,
    )
    pb = env["playbook"]
    # §17.881b — the pre-existing entry MUST survive the merge (the first cut's
    # deserializer dropped `playbook`, so every later write clobbered it; this
    # assertion is deliberately on an entry NOT present in the adds).
    assert pb["proven"] == ["EXISTING entry from an earlier step",
                            "tarball via servarr.com works"]
    assert pb["ruled_out"] == ["apt.servarr.com repo — unreachable from LXC"]


async def test_playbook_survives_a_plain_fact_fold():
    """§17.881b — a facts-only set_environment call (the every-submit path)
    must not erase the playbook."""
    from app.modules.assist_environment import set_environment
    db = AsyncMock()
    row = MagicMock()
    row.mappings.return_value.first.return_value = {"metadata": {
        "environment": {"facts": ["old fact"],
                        "playbook": {"proven": ["servarr updatefile pattern"]}}}}
    db.execute = AsyncMock(return_value=row)
    env = await set_environment(session_id="s", facts=["new fact"], db=db)
    assert env["playbook"] == {"proven": ["servarr updatefile pattern"]}
    assert "new fact" in env["facts"]


# ── renderer ─────────────────────────────────────────────────────────────


def test_render_playbook_block_binding_language():
    block = assist_render.render_playbook_block({
        "playbook": {"proven": ["P1"], "ruled_out": ["R1"]}})
    assert "BINDING" in block
    assert "P1" in block and "R1" in block
    assert "do NOT prescribe these again" in block


def test_render_playbook_block_empty_is_blank():
    assert assist_render.render_playbook_block({}) == ""
    assert assist_render.render_playbook_block({"playbook": {"proven": []}}) == ""


def test_session_memory_carries_playbook_and_survives_budget():
    env = {
        "profile": "root@pve single shell",
        "facts": [f"fact number {i} about the system with some length" for i in range(60)],
        "playbook": {"proven": ["<app>.servarr.com updatefile tarball works"],
                     "ruled_out": ["apt.servarr.com repo unreachable"]},
    }
    block = assist_render.render_session_memory(env, [], budget=2000)
    assert "Session playbook" in block
    assert "updatefile tarball works" in block  # never budget-dropped
    assert len(block) <= 2100


# ── reconcile apply ──────────────────────────────────────────────────────


async def test_reconcile_on_commit_retires_and_folds(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "assist_commit_reconcile_enabled", True)
    calls = {}

    async def fake_set_env(**kw):
        calls.update(kw)
        return {}

    fake_resp = SimpleNamespace()
    with patch("app.modules.assist_agent.get_environment",
               new=AsyncMock(return_value={"facts": [
                   "prowlarr is not installed in container 102",
                   "Host is Proxmox VE 9.2",
               ]})), \
         patch("app.modules.assist_agent.set_environment", new=fake_set_env), \
         patch("app.model_router.tool_call", new=AsyncMock(return_value=fake_resp)), \
         patch("app.utils.tool_call_args.read_tool_args", return_value={
             "retire_facts": ["prowlarr is not installed in container 102",
                              "NOT IN LEDGER — must be ignored"],
             "proven_methods": ["servarr updatefile tarball works"],
             "ruled_out_approaches": ["apt.servarr.com repo unreachable"],
         }):
        db = AsyncMock()
        row = MagicMock()
        row.mappings.return_value.first.return_value = {"title": "T", "prompt_template": "p"}
        db.execute = AsyncMock(return_value=row)
        res = await assist_memory.reconcile_on_commit(
            session_id="s", node_key="T14", evidence="service active; HTTP 200", db=db)
    assert res == {"retired": 1, "proven": 1, "ruled_out": 1}
    # only the VERBATIM ledger echo retired; hallucinated retire ignored
    assert calls["retract_facts"] == ["prowlarr is not installed in container 102"]
    assert calls["playbook_proven"] == ["servarr updatefile tarball works"]
    assert calls["playbook_ruled_out"] == ["apt.servarr.com repo unreachable"]


async def test_reconcile_valve_off_noop(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "assist_commit_reconcile_enabled", False)
    res = await assist_memory.reconcile_on_commit(
        session_id="s", node_key="T14", evidence="x", db=AsyncMock())
    assert res == {"retired": 0, "proven": 0, "ruled_out": 0}


# ── failure streak ───────────────────────────────────────────────────────


async def test_fix_failure_streak_counts_leading_fixes_and_extracts_commands():
    db = AsyncMock()
    row = MagicMock()
    row.mappings.return_value.all.return_value = [
        {"kind": "fix", "content": "## Fix\n```bash\ncurl -L https://bad.example\n```"},
        {"kind": "fix", "content": "try\n```bash\ntar -xzf /tmp/x.tar.gz\n```"},
        {"kind": "guide", "content": "walkthrough\n```bash\necho old\n```"},
        {"kind": "fix", "content": "older fix beyond the break"},
    ]
    db.execute = AsyncMock(return_value=row)
    streak, cmds = await _fix_failure_streak(session_id="s", node_key="T16", db=db)
    assert streak == 2
    assert "curl -L https://bad.example" in cmds and "tar -xzf" in cmds
    assert "echo old" not in cmds


async def test_fix_failure_streak_zero_when_latest_not_fix():
    db = AsyncMock()
    row = MagicMock()
    row.mappings.return_value.all.return_value = [
        {"kind": "guide", "content": "x"}, {"kind": "fix", "content": "y"}]
    db.execute = AsyncMock(return_value=row)
    streak, cmds = await _fix_failure_streak(session_id="s", node_key="T16", db=db)
    assert streak == 0 and cmds == ""
