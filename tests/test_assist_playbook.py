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


async def test_fix_failure_streak_counts_all_fixes_since_claim():
    """§17.882 — an interleaved Guide press must NOT reset the count (live: 5
    fixes, zero escalations, because a guide sat between them). The SQL filters
    to kind='fix' since presented_at; streak = all of them."""
    db = AsyncMock()
    presented = MagicMock(); presented.scalar.return_value = "2026-08-30T14:00:00Z"
    rows = MagicMock()
    rows.mappings.return_value.all.return_value = [
        {"content": "## Fix\n```bash\ncurl -L https://bad.example\n```"},
        {"content": "try\n```bash\ntar -xzf /tmp/x.tar.gz\n```"},
        {"content": "plain prose fix, no fence"},
    ]
    db.execute = AsyncMock(side_effect=[presented, rows])
    streak, cmds = await _fix_failure_streak(session_id="s", node_key="T16", db=db)
    assert streak == 3
    assert "curl -L https://bad.example" in cmds and "tar -xzf" in cmds


async def test_fix_failure_streak_zero_when_no_fix_turns():
    db = AsyncMock()
    presented = MagicMock(); presented.scalar.return_value = None
    rows = MagicMock(); rows.mappings.return_value.all.return_value = []
    db.execute = AsyncMock(side_effect=[presented, rows])
    streak, cmds = await _fix_failure_streak(session_id="s", node_key="T16", db=db)
    assert streak == 0 and cmds == ""


# ── §17.882 — code-enforced no-repeat gate ───────────────────────────────


def test_find_repeated_failed_detects_command_and_url():
    from app.modules.assist_guide import find_repeated_failed
    failed = 'curl -L "https://radarr.video/api/v1/update/master/updatefile?os=linux&arch=x64" -o /tmp/Radarr.tar.gz'
    out = ('## Fix\n```bash\ncurl -L "https://radarr.video/api/v1/update/master/'
           'updatefile?os=linux&arch=x64" -o /tmp/Radarr.tar.gz\n```')
    hits = find_repeated_failed(out, failed)
    assert hits  # the identical dead command is caught deterministically


def test_find_repeated_failed_clean_on_different_method():
    from app.modules.assist_guide import find_repeated_failed
    failed = 'curl -L "https://radarr.video/api/v1/update" -o /tmp/R.tar.gz'
    out = ('```bash\ncurl -fsSL https://api.github.com/repos/Radarr/Radarr/'
           'releases/latest\n```')
    assert find_repeated_failed(out, failed) == []


def test_error_focus_query_picks_error_line():
    from app.modules.assist_guide import _error_focus_query
    err = ("root@radarr:~# curl -L ...\n"
           "  % Total ...\n"
           "gzip: stdin: not in gzip format\n"
           "tar: Child returned status 1\n")
    q = _error_focus_query("Install Radarr in LXC 103", err)
    assert "not in gzip format" in q and "Install Radarr" in q
    assert len(q) <= 130




def _fix_ctx():
    from app.modules.assist_guide import StepContext
    return StepContext(
        node_key="T16", title="Install Radarr in LXC 103", tool="Shell",
        domain=None, system_prompt="sys", base_prompt="install radarr",
        upstream_outputs={}, upstream_truncated_keys=[], grounding="",
        grounding_kind=None, assembled_prompt="## Task\ninstall radarr",
    )

@pytest.mark.asyncio
async def test_generate_fix_regen_gate_blocks_repeat():
    """First draw repeats the failed command → ONE regeneration; the clean
    regen is returned with no warning banner."""
    from app.modules import assist_guide
    from app.modules.assist_guide import generate_fix, StepContext
    failed = 'curl -L "https://radarr.video/api/v1/update" -o /tmp/R.tar.gz'
    bad = f'## Fix\n```bash\n{failed}\n```'
    good = '## Fix\n```bash\ncurl -fsSL https://api.github.com/repos/Radarr/Radarr/releases/latest\n```'
    draws = [SimpleNamespace(text=bad, success=True, error=None, model="m", raw={}),
             SimpleNamespace(text=good, success=True, error=None, model="m", raw={})]
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(side_effect=draws)) as chat:
        res = await generate_fix(
            ctx=_fix_ctx(), error_text="gzip: stdin: not in gzip format",
            research=False, node_key="T16",
            failure_streak=2, failed_commands=failed,
        )
    assert chat.await_count == 2
    assert "api.github.com" in res["fix"]
    assert "Repeat warning" not in res["fix"]
    assert res["guidance_meta"]["repeat_violations"] == []


@pytest.mark.asyncio
async def test_generate_fix_warning_banner_when_regen_still_repeats():
    from app.modules import assist_guide
    from app.modules.assist_guide import generate_fix, StepContext
    failed = 'curl -L "https://radarr.video/api/v1/update" -o /tmp/R.tar.gz'
    bad = f'## Fix\n```bash\n{failed}\n```'
    draws = [SimpleNamespace(text=bad, success=True, error=None, model="m", raw={}),
             SimpleNamespace(text=bad, success=True, error=None, model="m", raw={})]
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(side_effect=draws)):
        res = await generate_fix(
            ctx=_fix_ctx(), error_text="gzip: stdin: not in gzip format",
            research=False, node_key="T16",
            failure_streak=1, failed_commands=failed,
        )
    assert res["fix"].startswith("⚠️ **Repeat warning:**")
    assert res["guidance_meta"]["repeat_violations"]


# ── §17.882 — plan correction from ruled-out lessons ─────────────────────


@pytest.mark.asyncio
async def test_plan_correction_stages_proposal_on_domain_match():
    from app.modules.assist_memory import _propose_plan_correction
    db = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.all.return_value = [
        {"node_key": "T17", "prompt_template": "Install Sonarr via the apt.servarr.com repository"},
        {"node_key": "T18", "prompt_template": "Configure quality profiles in the UI"},
    ]
    db.execute = AsyncMock(return_value=rows)
    staged = {}
    async def fake_stage(**kw):
        staged.update(kw)
    with patch("app.modules.assist_notes._stage_replan_proposal", new=fake_stage):
        await _propose_plan_correction(
            session_id="s",
            ruled=["apt.servarr.com apt repository — fails to resolve DNS inside the containers"],
            proven=["GitHub tarball install to /opt with systemd unit"],
            db=db,
        )
    assert [a["node_key"] for a in staged["affected"]] == ["T17"]
    assert "GitHub tarball" in staged["affected"][0]["required_change"]
    assert staged["note_kind"] == "constraint"


@pytest.mark.asyncio
async def test_plan_correction_silent_when_no_match():
    from app.modules.assist_memory import _propose_plan_correction
    db = AsyncMock()
    rows = MagicMock()
    rows.mappings.return_value.all.return_value = [
        {"node_key": "T18", "prompt_template": "Configure quality profiles"}]
    db.execute = AsyncMock(return_value=rows)
    called = {}
    async def fake_stage(**kw):
        called["yes"] = True
    with patch("app.modules.assist_notes._stage_replan_proposal", new=fake_stage):
        await _propose_plan_correction(
            session_id="s", ruled=["apt.servarr.com repo dead"], proven=[], db=db)
    assert not called
