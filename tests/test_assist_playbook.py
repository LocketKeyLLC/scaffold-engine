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
    rows = MagicMock()
    rows.mappings.return_value.all.return_value = [
        {"content": "## Fix\n```bash\ncurl -L https://bad.example\n```"},
        {"content": "try\n```bash\ntar -xzf /tmp/x.tar.gz\n```"},
        {"content": "plain prose fix, no fence"},
    ]
    # §17.886(#6) — ONE node-scoped query; no presented_at filter (claim-repair
    # re-stamps must not zero the streak).
    db.execute = AsyncMock(return_value=rows)
    streak, cmds = await _fix_failure_streak(session_id="s", node_key="T16", db=db)
    assert streak == 3
    assert "curl -L https://bad.example" in cmds and "tar -xzf" in cmds


async def test_fix_failure_streak_zero_when_no_fix_turns():
    db = AsyncMock()
    rows = MagicMock(); rows.mappings.return_value.all.return_value = []
    db.execute = AsyncMock(return_value=rows)
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
    assert res["fix"].startswith("⚠️ **Caution:**")  # §17.883 unified banner
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


def test_find_repeated_failed_ignores_local_verification_urls():
    """§17.882b — localhost/RFC1918 health-check URLs recur legitimately in
    every fix; only EXTERNAL URLs (the dead download hosts) are repeat signal."""
    from app.modules.assist_guide import find_repeated_failed
    failed = ('curl -L "https://radarr.video/api/v1/update" -o /tmp/R.tar.gz\n'
              'curl -s http://localhost:7878\n'
              'curl -s http://192.168.1.21:9696')
    out = ('```bash\ncurl -fsSL https://api.github.com/repos/Radarr/Radarr/releases/latest\n```\n'
           'then verify:\n```bash\ncurl -s -o /dev/null -w "%{http_code}" http://localhost:7878\n```')
    assert find_repeated_failed(out, failed) == []


# ── §17.883 — URL provenance + variation skeletons ───────────────────────


def test_url_skeleton_matches_version_guess_variations():
    from app.modules.assist_guide import _url_skeleton
    a = _url_skeleton("https://github.com/Radarr/Radarr/releases/latest/download/Radarr.master.linux-core-x64.tar.gz")
    b = _url_skeleton("https://github.com/Radarr/Radarr/releases/download/v5.3.3/Radarr.master.linux-core-x64.tar.gz")
    c = _url_skeleton("https://github.com/Radarr/Radarr/releases/download/v5.3.0/Radarr.master.linux-core-x64.tar.gz")
    assert a == b == c
    d = _url_skeleton("https://github.com/Sonarr/Sonarr/releases/latest/download/S.tar.gz")
    assert d != a


def test_find_repeated_failed_catches_version_variation():
    """The live guess-cycle: three 'different' URLs, one failing endpoint
    family — all now count as repeats."""
    from app.modules.assist_guide import find_repeated_failed
    failed = 'curl -L "https://github.com/Radarr/Radarr/releases/latest/download/Radarr.master.linux-core-x64.tar.gz" -o /tmp/R.tar.gz'
    out = ('```bash\ncurl -L "https://github.com/Radarr/Radarr/releases/download/'
           'v5.3.0/Radarr.master.linux-core-x64.tar.gz" -o /tmp/R.tar.gz\n```')
    assert find_repeated_failed(out, failed)


def test_find_novel_urls_flags_ungrounded_and_passes_grounded():
    from app.modules.assist_guide import find_novel_urls
    corpus = ("## Research\n[1] (web: https://wiki.servarr.com/radarr/installation) "
              "query: install\nSome content mentioning "
              "https://github.com/Radarr/Radarr/releases as the release page.")
    draft = ("```bash\ncurl -L https://github.com/Radarr/Radarr/releases/download/"
             "v9.9.9/invented.tar.gz -o /tmp/R.tar.gz\n```\n"
             "```bash\ncurl -s https://api.github.com/repos/Radarr/Radarr/releases/latest | grep browser_download_url\n```\n"
             "See https://wiki.servarr.com/radarr/installation "
             "and check http://localhost:7878 after.")
    novel = find_novel_urls(draft, corpus)
    assert any("v9.9.9" in n for n in novel)          # invented + consumed → flagged
    assert all("api.github.com" not in n for n in novel)  # read-only discovery → exempt
    assert all("wiki.servarr.com" not in n for n in novel)  # grounded/prose → clean
    assert all("localhost" not in n for n in novel)   # local → exempt


@pytest.mark.asyncio
async def test_generate_fix_novel_url_gate_regenerates():
    """A draft with an invented URL at escalation triggers ONE regen; the
    discovery-command regen (no external URLs) is returned clean."""
    from app.modules import assist_guide
    from app.modules.assist_guide import generate_fix
    invented = '## Fix\n```bash\ncurl -L https://github.com/Radarr/Radarr/releases/download/v9.9.9/x.tar.gz -o /tmp/R.tar.gz\n```'
    discovery = ('## Fix\n```bash\ncurl -s https://api.github.com/repos/Radarr/Radarr/releases/latest '
                 '| grep browser_download_url\n```')
    convo = ""  # §17.883b — read-only discovery needs no provenance
    draws = [SimpleNamespace(text=invented, success=True, error=None, model="m", raw={}),
             SimpleNamespace(text=discovery, success=True, error=None, model="m", raw={})]
    with patch.object(assist_guide.model_router, "chat",
                      new=AsyncMock(side_effect=draws)) as chat:
        res = await generate_fix(
            ctx=_fix_ctx(), error_text="tar: not in gzip format",
            research=False, node_key="T16",
            failure_streak=3, failed_commands="curl -L https://radarr.video/api/x -o /tmp/R.tar.gz",
            conversation=convo,
        )
    assert chat.await_count == 2
    assert "api.github.com" in res["fix"]
    assert "Caution" not in res["fix"]
    assert res["guidance_meta"]["novel_url_violations"] == []


# ── §17.887 — guide integrity gate (#8) ──────────────────────────────────


def test_guide_integrity_warning_flags_repeat_and_novel():
    from app.modules.assist_guide import guide_integrity_warning
    failed = 'curl -L "https://radarr.video/api/x" -o /tmp/R.tar.gz'
    guide = ('```bash\ncurl -L "https://radarr.video/api/x" -o /tmp/R.tar.gz\n```\n'
             '```bash\nwget https://invented.example/pkg.tar.gz\n```')
    warn = guide_integrity_warning(guide, "no grounding here", failed)
    # §17.906 — wording: a remedy that ran cleanly but did not resolve the
    # step is "already tried", not "already failed".
    assert "ALREADY TRIED" in warn and "not traceable" in warn


def test_guide_integrity_warning_clean_guide_silent():
    from app.modules.assist_guide import guide_integrity_warning
    guide = '```bash\nsystemctl status radarr\n```'
    assert guide_integrity_warning(guide, "corpus", "") == ""


# ── §17.906 — the command half of the no-repeat gate was DEAD ────────────
#
# find_repeated_failed wrapped the whole "\n\n"-joined failed-command list in
# ONE fence, so _normalized_commands collapsed every failed command into a
# single whitespace-joined blob. Set-membership could then never match an
# individual command, so the gate returned [] whenever MORE THAN ONE command
# had failed — i.e. in every real troubleshooting session. Every pre-existing
# test used exactly one failed command, which is why it survived. Live cost
# (session 613dd1df / T23): `qm destroy 106 --purge` prescribed FOUR times with
# no warning banner across a 27-hour Ubuntu-install loop.


def test_find_repeated_failed_catches_repeat_in_multi_command_window():
    """THE regression: the same draft, one vs. many failed commands."""
    from app.modules.assist_guide import find_repeated_failed
    draft = "## Do this next\n```bash\nqm destroy 106 --purge\n```"
    one = "qm destroy 106 --purge"
    many = ("qm destroy 106 --purge\n\nqm start 106\n\n"
            "qm set 106 --scsi0 local-lvm:100\n\nlsblk")
    assert find_repeated_failed(draft, one) == ["qm destroy 106 --purge"]
    # pre-§17.906 this returned [] purely because the window held >1 command
    assert find_repeated_failed(draft, many) == ["qm destroy 106 --purge"]


def test_find_repeated_failed_catches_line_lifted_into_new_block():
    """A failed one-liner re-prescribed as one line of a fresh multi-line
    block: block-only granularity matched nothing (live turn 1382 re-issued
    `qm set 106 --boot order=ide2` this way, after it had already errored with
    'device ide2 does not exist')."""
    from app.modules.assist_guide import find_repeated_failed
    failed = "qm set 106 --boot order=ide2\n\nqm destroy 106 --purge"
    draft = ("```bash\nqm set 106 --scsi0 local-lvm:100\n"
             "qm set 106 --ide2 local:iso/ubuntu-22.04.3-live-server-amd64.iso,media=cdrom\n"
             "qm set 106 --boot order=ide2\n```")
    assert "qm set 106 --boot order=ide2" in find_repeated_failed(draft, failed)


def test_find_repeated_failed_exempts_readonly_diagnostics():
    """Re-running a DISCOVERY command is the behaviour the escalation directive
    demands ('lead with a discovery command'), so it must never count as a
    repeat — otherwise the gate fights the fix it exists to enable."""
    from app.modules.assist_guide import find_repeated_failed
    failed = ("qm config 106\n\npvesm list local --content iso\n\n"
              "qm destroy 106 --purge")
    draft = "```bash\nqm config 106\n```\nthen:\n```bash\npvesm list local --content iso\n```"
    assert find_repeated_failed(draft, failed) == []


def test_is_readonly_command_classifies_verbs_and_pairs():
    from app.modules.assist_guide import _is_readonly_command
    for ro in ("qm config 106", "sudo lsblk", "pct config 104", "systemctl status radarr",
               "pvesm list local --content iso", "cat /etc/fstab", "# a comment",
               "curl -s http://localhost:7878"):
        assert _is_readonly_command(ro), ro
    # a compound is read-only only if EVERY segment is
    assert _is_readonly_command("sudo apt update")
    assert _is_readonly_command("qm config 106 && lsblk")
    for mutating in ("sudo apt update && sudo apt install -y openssh-server",
                     "qm config 106 && qm set 106 --boot order=scsi0",
                     "qm set 106 --boot order=ide2", "qm destroy 106 --purge",
                     "sudo apt install -y steamcmd", "rm -rf /opt/Radarr",
                     'curl -L "https://x/y.tar.gz" -o /tmp/y.tar.gz',
                     "curl -fsSL https://get.docker.com | sh"):
        assert not _is_readonly_command(mutating), mutating


def test_find_repeated_failed_still_clean_on_genuine_method_change():
    """Guard against the fix over-firing: a materially different approach over
    a multi-command window must stay silent."""
    from app.modules.assist_guide import find_repeated_failed
    failed = ("qm destroy 106 --purge\n\nqm set 106 --boot order=ide2\n\n"
              "qm start 106")
    draft = ("```bash\nqm set 106 --args '-device virtio-net'\n```\n"
             "```bash\nvirt-install --location /var/lib/vz/template/iso/x.iso\n```")
    assert find_repeated_failed(draft, failed) == []


async def test_note_endpoint_dedupes_identical_notes():
    """§17.906 — the decide path routes note-shaped messages at POST
    /assist/{sid}/note, which recorded WITHOUT dedupe: live session 613dd1df
    carried the identical "wants to build a markdown linter" addition four
    times, and every note rides every later prompt."""
    import inspect
    from app.routers import assist as assist_router
    src = inspect.getsource(assist_router.assist_note)
    assert "dedupe=True" in src, "the /note endpoint must dedupe identical notes"


# ── §17.906 — shell-metacharacter lint ───────────────────────────────────
#
# Live T23 (turns 1334-1337): the engine emitted
#   qm set 106 --boot order=scsi0;net0;cdrom
# bash split it at each `;`, Proxmox saw only `order=scsi0`, and `net0`/`cdrom`
# ran as commands ("-bash: net0: command not found"). The engine's next turn
# blamed the OPERATOR ("you used semicolons") and its comma "correction" was
# also wrong — three turns lost to a defect the engine authored itself.


def test_find_shell_unsafe_catches_the_live_defect():
    from app.modules.assist_guide import find_shell_unsafe_commands
    hits = find_shell_unsafe_commands(
        "```bash\nqm set 106 --boot order=scsi0;net0;cdrom\n```")
    assert len(hits) == 1
    assert hits[0]["token"] == "order=scsi0;net0;cdrom"


def test_find_shell_unsafe_catches_unquoted_url_ampersand():
    from app.modules.assist_guide import find_shell_unsafe_commands
    assert find_shell_unsafe_commands(
        "```bash\ncurl http://h/a?x=1&y=2 -o /tmp/f\n```")
    # quoting it makes it correct
    assert find_shell_unsafe_commands(
        '```bash\ncurl "http://h/a?x=1&y=2" -o /tmp/f\n```') == []


def test_find_shell_unsafe_silent_on_legitimate_shell():
    """Over-firing here would be worse than the bug: these are all correct."""
    from app.modules.assist_guide import find_shell_unsafe_commands
    for ok in (
        "```bash\nqm set 106 --boot order=scsi0,net0,cdrom\n```",      # commas
        "```bash\nqm create 106 --net0 virtio,bridge=vmbr0\n```",      # value w/ =
        "```bash\nqm set 106 --ide2 local:iso/u.iso,media=cdrom\n```",
        "```bash\nFOO=1; echo $FOO\n```",          # TRAILING ; is a separator
        "```bash\nsudo apt update && sudo apt install -y steamcmd\n```",
        r"```bash" "\n" r"find . -name '*.txt' -exec rm {} \;" "\n" r"```",  # escaped \;
        "```bash\n# note: order=a;b\n```",                     # comment
        "```bash\njournalctl -u radarr | grep -i error\n```",  # pipe, no '='
    ):
        assert find_shell_unsafe_commands(ok) == [], ok


def test_guide_integrity_warning_flags_shell_unsafe():
    from app.modules.assist_guide import guide_integrity_warning
    warn = guide_integrity_warning(
        "```bash\nqm set 106 --boot order=scsi0;net0;cdrom\n```", "corpus", "")
    assert "SPLIT" in warn
