"""§17.1186 — Auto mode's hands-on steps go through the supervised write
channel: drafted, gated, parked for approval, run on approval, verified."""
from __future__ import annotations

import inspect
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.config import settings
from app.modules import supervised_runs as sr
from app.modules import execution_agent as ea
from app.modules import decision_pause as dp

RUNBOOK = """## Prerequisites
- Container 111 exists.

## Run this
1. Start it:
```bash
pct start 111
```
2. Set DNS:
```bash
pct set 111 --nameserver 192.168.1.1
```

## Verify
- `pct status 111` reports `status: running`.
- Resolution works: `pct exec 111 -- getent hosts example.org`.

## Rollback
```bash
pct stop 111
```
"""
POLICY = {"allow": ["pct start", "pct set"], "sudo": True, "helper": "11"}
NODE = {"node_key": "ADD50", "title": "Start container 111", "prompt_template": "Done when `pct status 111` reports running.",
        "depends_on": [], "tool": "LLM", "node_type": "task", "hands_on_reason": "observed:pct status 111"}


def _spec():
    s = MagicMock(); s.name = "pve-runner"; s.headers = {"X-Runner-Token": "tok"}; return s


# ── the runbook's commands ───────────────────────────────────────────────

def test_run_commands_come_from_run_this_and_verify_from_verify_only():
    assert sr.runbook_commands(RUNBOOK) == ["pct start 111", "pct set 111 --nameserver 192.168.1.1"]
    assert sr.verify_commands(RUNBOOK) == ["pct status 111", "pct exec 111 -- getent hosts example.org"]


def test_a_runbook_without_sections_uses_every_fence():
    assert sr.runbook_commands("```\npct start 111\n```\ntext\n```\npct status 111\n```") == ["pct start 111", "pct status 111"]
    assert sr.verify_commands("no verify section here `pct status 1`") == []


def test_frame_offers_run_only_when_the_gate_is_clean():
    f = sr.frame_run(NODE, RUNBOOK, _spec(), POLICY)
    assert f["kind"] == "run" and [o["id"] for o in f["options"]] == ["run", "myself", "skip"] and f["suggested"] == "run"
    assert f["commands"] == ["pct start 111", "pct set 111 --nameserver 192.168.1.1"] and f["refused"] == []
    assert f["verify"] == ["pct status 111", "pct exec 111 -- getent hosts example.org"] and f["runner"] == "pve-runner"
    f2 = sr.frame_run(NODE, RUNBOOK, _spec(), {"allow": ["pct start"], "sudo": False})
    assert [o["id"] for o in f2["options"]] == ["myself", "skip"] and f2["suggested"] == "myself"
    assert f2["refused"][0]["command"].startswith("pct set") and "cannot run it as written" in f2["question"]


# ── what is waiting ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_pending_hands_on_skips_decided_and_decision_nodes_and_human_steps():
    db = AsyncMock()
    r1 = MagicMock(); r1.scalar.return_value = {"ADD50": {"by": "operator", "choice": "myself"}}
    r2 = MagicMock(); r2.mappings.return_value.all.return_value = [
        {"node_key": "ADD50", "title": "Start container 111", "prompt_template": "Done when `pct status 111` reports running.",
         "depends_on": [], "tool": "LLM", "node_type": "task", "description": None, "retry_count": 0, "last_verification_reason": None},
        {"node_key": "T2", "title": "Decide", "prompt_template": "Done when `pct status 1` shows x.", "depends_on": [], "tool": "LLM",
         "node_type": "decision", "description": None, "retry_count": 0, "last_verification_reason": None},
        {"node_key": "H1", "title": "Ask", "prompt_template": "", "depends_on": [], "tool": "human", "node_type": "task",
         "description": None, "retry_count": 0, "last_verification_reason": None},
        {"node_key": "T9", "title": "Write docs", "prompt_template": "Write it.", "depends_on": [], "tool": "LLM", "node_type": "task",
         "description": None, "retry_count": 0, "last_verification_reason": None},
        {"node_key": "ADD88", "title": "Install Caddy", "prompt_template": "Write the file via `tee -a`.", "depends_on": [], "tool": "LLM",
         "node_type": "task", "description": None, "retry_count": 0, "last_verification_reason": None},
    ]
    db.execute = AsyncMock(side_effect=[r1, r2])
    with patch.object(settings, "shell_tool_enabled", False), patch.object(settings, "mcp_tool_enabled", True):
        node = await sr.pending_hands_on(db, "j")
    assert node["node_key"] == "ADD88" and node["hands_on_reason"] == "writes:tee -a"
    sql = db.execute.await_args_list[1].args[0].text
    assert "status = 'pending'" in sql and "NOT EXISTS" in sql and "ORDER BY n.execution_order" in sql


@pytest.mark.asyncio
async def test_channel_is_none_when_off_or_without_a_runner_or_policy(monkeypatch):
    monkeypatch.setattr(settings, "execution_supervised_runs_enabled", False)
    assert await sr.channel(AsyncMock()) is None
    monkeypatch.setattr(settings, "execution_supervised_runs_enabled", True)
    monkeypatch.setattr(settings, "mcp_tool_enabled", True)
    with patch("app.modules.assist_local_runner.runner_spec", new=AsyncMock(return_value=None)):
        assert await sr.channel(AsyncMock()) is None
    with patch("app.modules.assist_local_runner.runner_spec", new=AsyncMock(return_value=_spec())), \
         patch("app.modules.assist_supervised.write_policy", new=AsyncMock(return_value=None)):
        assert await sr.channel(AsyncMock()) is None
    with patch("app.modules.assist_local_runner.runner_spec", new=AsyncMock(return_value=_spec())), \
         patch("app.modules.assist_supervised.write_policy", new=AsyncMock(return_value=POLICY)), \
         patch("app.modules.runner_secrets.names", new=AsyncMock(return_value=["DB_PASSWORD"])):
        spec, pol = await sr.channel(AsyncMock())
    assert spec.name == "pve-runner" and {k: v for k, v in pol.items() if k != "held"} == POLICY
    assert pol["held"] == ["DB_PASSWORD"]          # §17.1193 — names the ENGINE holds


@pytest.mark.asyncio
async def test_the_write_channel_survives_a_missing_secret_store(monkeypatch):
    """The held-names look-up is an EXTRA. A missing table (the migration has
    not run yet) must not make the whole write channel disappear — that turns a
    schema lag into 'the engine silently stopped being able to run anything'."""
    monkeypatch.setattr(settings, "execution_supervised_runs_enabled", True)
    monkeypatch.setattr(settings, "mcp_tool_enabled", True)
    with patch("app.modules.assist_local_runner.runner_spec", new=AsyncMock(return_value=_spec())), \
         patch("app.modules.assist_supervised.write_policy", new=AsyncMock(return_value=POLICY)), \
         patch("app.modules.runner_secrets.names", new=AsyncMock(side_effect=RuntimeError("no such table"))):
        ch = await sr.channel(AsyncMock())
    assert ch is not None and ch[1]["allow"] == POLICY["allow"] and ch[1]["held"] == []


# ── resolution ───────────────────────────────────────────────────────────

def _db(rowcounts):
    db = AsyncMock()
    results = []
    for rc in rowcounts:
        r = MagicMock(); r.rowcount = rc; results.append(r)
    db.execute = AsyncMock(side_effect=results)
    return db


@pytest.mark.asyncio
async def test_skip_and_myself_write_the_node_without_touching_the_runner():
    waiting = {"kind": "run", "runbook": RUNBOOK, "commands": ["pct start 111"], "verify": [], "refused": []}
    db = _db([1])
    out = await sr.resolve_run(db, "j", "ADD50", "skip", waiting)
    assert out["outcome"] == "skipped" and "status = 'skipped'" in db.execute.await_args.args[0].text
    db = _db([1])
    out = await sr.resolve_run(db, "j", "ADD50", "myself", waiting)
    assert out["outcome"] == "runbook" and db.execute.await_args.args[1]["out"] == RUNBOOK
    assert (await sr.resolve_run(_db([]), "j", "ADD50", "explode", waiting))["outcome"] == "bad_choice"


@pytest.mark.asyncio
async def test_run_claims_runs_verifies_and_marks_done_with_the_report(monkeypatch):
    waiting = {"kind": "run", "runbook": RUNBOOK, "commands": ["pct start 111", "pct set 111 --nameserver 192.168.1.1"],
               "verify": ["pct status 111"], "refused": []}
    db = _db([1, 1])
    executed = [{"command": "pct start 111", "output": "", "exit": 0, "ok": True, "approval_id": "a", "refused": False},
                {"command": "pct set 111 --nameserver 192.168.1.1", "output": "", "exit": 0, "ok": True, "approval_id": "b", "refused": False}]
    with patch.object(sr, "channel", new=AsyncMock(return_value=(_spec(), POLICY))), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=executed)) as rb, \
         patch("app.modules.assist_local_runner.run_probes", new=AsyncMock(return_value=("== V1 ==\nstatus: running\n", [{"id": "V1", "command": "pct status 111", "ok": True, "chars": 15}]))):
        out = await sr.resolve_run(db, "j", "ADD50", "run", waiting)
    assert out["outcome"] == "ran" and out["node_status"] == "done"
    assert rb.await_args.args[1] == ["pct start 111", "pct set 111 --nameserver 192.168.1.1"]
    claim_sql = db.execute.await_args_list[0].args[0].text
    assert "SET status = 'running'" in claim_sql and "status = 'pending'" in claim_sql
    done_sql, done_params = db.execute.await_args_list[1].args
    assert "SET status = 'done'" in done_sql.text and "status = 'running'" in done_sql.text
    assert "## Executed on pve-runner" in done_params["out"] and "$ pct start 111" in done_params["out"]
    assert "## Verify results\n$ pct status 111\nstatus: running" in done_params["out"]


@pytest.mark.asyncio
async def test_a_failed_command_fails_the_node_with_the_reason():
    waiting = {"kind": "run", "runbook": RUNBOOK, "commands": ["pct start 111", "pct set 111 --nameserver 192.168.1.1"], "verify": ["pct status 111"], "refused": []}
    db = _db([1, 1])
    executed = [{"command": "pct start 111", "output": "CT 111 does not exist", "exit": 2, "ok": False, "approval_id": "a", "refused": False}]
    with patch.object(sr, "channel", new=AsyncMock(return_value=(_spec(), POLICY))), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=executed)), \
         patch("app.modules.assist_local_runner.run_probes", new=AsyncMock()) as probes:
        out = await sr.resolve_run(db, "j", "ADD50", "run", waiting)
    assert out["outcome"] == "failed" and "exited 2" in out["reason"]
    probes.assert_not_awaited()                                   # no verify after a failure
    sql, params = db.execute.await_args_list[1].args
    assert "SET status = 'failed'" in sql.text and params["why"].startswith("supervised run stopped")


@pytest.mark.asyncio
async def test_run_is_refused_when_the_frame_carried_refusals_or_the_channel_closed():
    waiting = {"kind": "run", "runbook": RUNBOOK, "commands": ["pct destroy 111"], "verify": [], "refused": [{"command": "pct destroy 111", "why": "x"}]}
    assert (await sr.resolve_run(_db([]), "j", "ADD50", "run", waiting))["outcome"] == "not_runnable"
    waiting["refused"] = []
    with patch.object(sr, "channel", new=AsyncMock(return_value=None)):
        assert (await sr.resolve_run(_db([]), "j", "ADD50", "run", waiting))["outcome"] == "no_channel"
    with patch.object(sr, "channel", new=AsyncMock(return_value=(_spec(), POLICY))):
        out = await sr.resolve_run(_db([]), "j", "ADD50", "run", waiting)   # pct destroy is not on the list any more
    assert out["outcome"] == "not_runnable" and out["refused"][0]["command"] == "pct destroy 111"


@pytest.mark.asyncio
async def test_resolve_decision_dispatches_a_run_pause_and_records_the_result():
    waiting = {"kind": "run", "node_key": "ADD50", "runbook": RUNBOOK, "commands": ["pct start 111"], "verify": [], "refused": []}
    db = AsyncMock()
    row = MagicMock(); row.mappings.return_value.first.return_value = {"status": "awaiting_decision", "metadata": {"awaiting_decision": waiting}}
    meta = MagicMock(); meta.rowcount = 1
    db.execute = AsyncMock(side_effect=[row, meta])
    with patch("app.modules.supervised_runs.resolve_run", new=AsyncMock(return_value={"outcome": "ran", "node_status": "done"})) as rr, \
         patch("app.modules.decision_pause.transition", new=AsyncMock(return_value=True)) as tr:
        out = await dp.resolve_decision(db, "j", "ADD50", choice="run", note="go")
    assert rr.await_args.args[3] == "run" and out["outcome"] == "ran" and out["record"]["node_status"] == "done"
    assert json.loads(db.execute.await_args_list[1].args[1]["patch"])["decisions"]["ADD50"]["result"] == "ran"
    assert tr.await_args.kwargs["to"] == "executing"


# ── the executor is wired ────────────────────────────────────────────────

def test_executor_wiring():
    pause = inspect.getsource(ea._pause_for_decision)
    assert "supervised_runs.channel(db)" in pause and "pending_hands_on(db, job_id)" in pause and "frame_run(" in pause
    assert "draft_runbook(" in pause and 'logger.warning("supervised_run_parked' in pause
    serial = inspect.getsource(ea.execute_all_nodes)
    assert "_hands_on_peek(node)" in serial and 'status in ("stale", "needs_approval")' in serial
    par = inspect.getsource(ea._run_parallel_frontier)
    assert "exclude=held" in par and 'status == "needs_approval"' in par and "held.add(" in par
    claim = inspect.getsource(ea._claim_ready_nodes_sql)
    assert "AND NOT (n.node_key = ANY(:exclude))" in claim
    seam = inspect.getsource(ea.execute_next_node)
    assert "_hand_back_for_approval(db, job_id, node, tool)" in seam
    assert seam.index("hands_on_step_as_runbook") < seam.index("_hand_back_for_approval(") < seam.index("NotImplementedError(")
    hb = inspect.getsource(ea._hand_back_for_approval)
    assert '"status": "needs_approval"' in hb and "SET status = 'pending', started_at = NULL" in hb and "status = 'running'" in hb
    gate = inspect.getsource(ea._classify_dag_executability)
    assert "supervised_runs.channel(db)" in gate and 'not why.startswith("tool:human")' in gate


@pytest.mark.asyncio
async def test_gate_counts_hands_on_steps_as_executable_when_the_channel_is_open(monkeypatch):
    monkeypatch.setattr(settings, "shell_tool_enabled", False)
    monkeypatch.setattr(settings, "hands_on_assist_gate_threshold", 0.5)
    db = AsyncMock(); res = MagicMock()
    res.mappings.return_value.all.return_value = [
        {"tool": "Shell", "node_type": "task", "node_key": "T1", "title": "x", "prompt_template": ""},
        {"tool": "LLM", "node_type": "task", "node_key": "T2", "title": "Start", "prompt_template": "Done when `pct status 111` reports running."},
        {"tool": "human", "node_type": "task", "node_key": "T3", "title": "ask", "prompt_template": ""},
    ]
    db.execute = AsyncMock(return_value=res)
    with patch("app.modules.supervised_runs.channel", new=AsyncMock(return_value=(_spec(), POLICY))):
        cls = await ea._classify_dag_executability(db, "j")
    assert cls["nonexec"] == 1 and cls["hands_on"] is False          # only the human step
    with patch("app.modules.supervised_runs.channel", new=AsyncMock(return_value=None)):
        cls = await ea._classify_dag_executability(db, "j")
    assert cls["nonexec"] == 3 and cls["hands_on"] is True


# ── §17.1189 — what the channel can actually carry ───────────────────────

HEREDOC_RUNBOOK = """## Run this
1. Write the unit file:
```bash
pct exec 106 -- bash -c 'cat > /etc/systemd/system/palworld.service <<EOF
[Unit]
Description=PalWorld Dedicated
[Service]
ExecStart=/opt/pal/start.sh
WorkingDirectory=/opt/pal
EOF'
```
2. Start it:
```bash
pct exec 106 -- systemctl start palworld
```

## Verify
- `pct exec 106 -- systemctl is-active palworld` reports `active`.
"""

LOOP_RUNBOOK = """## Run this
```bash
for i in 1 2 3; do
  pct set 10$i --nameserver 192.168.1.1
done
```
"""


def test_heredoc_fence_is_one_command_not_its_body_lines():
    """A unit-file body is not a list of commands. Before this, the frame's
    refusal list read `[Unit]`, `Description=…`, `ExecStart=…` — text the
    model never meant as commands, gated as if it had."""
    cmds = sr.runbook_commands(HEREDOC_RUNBOOK)
    assert len(cmds) == 2, cmds
    assert cmds[0].startswith("pct exec 106 -- bash -c") and "<<EOF" in cmds[0]
    assert cmds[1] == "pct exec 106 -- systemctl start palworld"
    for fragment in ("[Unit]", "Description=PalWorld Dedicated", "ExecStart=/opt/pal/start.sh",
                     "WorkingDirectory=/opt/pal", "EOF'"):
        assert fragment not in cmds, f"{fragment!r} was gated as a command"


def test_heredoc_block_is_refused_once_for_the_real_reason():
    from app.modules.assist_supervised import gate_block
    runnable, refused = gate_block(sr.runbook_commands(HEREDOC_RUNBOOK), ["pct exec"])
    assert [r["why"] for r in refused] == ["substitution/heredoc"], refused
    assert runnable == ["pct exec 106 -- systemctl start palworld"]


def test_multiline_loop_is_kept_whole():
    cmds = sr.runbook_commands(LOOP_RUNBOOK)
    assert len(cmds) == 1 and cmds[0].startswith("for i in 1 2 3; do")
    assert "done" in cmds[0]


def test_single_line_commands_are_still_split_per_line():
    assert sr.runbook_commands(RUNBOOK) == ["pct start 111", "pct set 111 --nameserver 192.168.1.1"]


@pytest.mark.asyncio
async def test_draft_runbook_redraws_an_empty_thinking_draw():
    """§17.1126 — model_general is a thinking model and this is a free-form
    generate, so a tight budget returns success=True with empty text. The
    draft must not hand the frame an empty runbook (no commands → the step
    silently falls to 'I'll do it myself')."""
    calls: list[dict] = []

    async def fake_generate(prompt, **kw):
        calls.append(kw)
        text_out = "" if len(calls) == 1 else "## Run this\n```bash\npct start 111\n```"
        return MagicMock(text=text_out, success=True)

    with patch("app.model_router.generate", new=fake_generate):
        out = await sr.draft_runbook({"node_key": "T1", "title": "start it"}, {"description": "brief"})
    assert "pct start 111" in out
    assert len(calls) >= 2, "an empty draw was accepted"
    assert calls[0]["max_tokens"] == settings.node_generation_max_tokens, calls[0]["max_tokens"]
    # the reasoning trace is discarded on the generate path (§17.683), so it is
    # off from the FIRST draw — not only as a rescue after an empty one
    assert calls[0]["think"] is False, calls[0]


@pytest.mark.asyncio
async def test_draft_runbook_tells_the_model_what_the_channel_can_carry():
    seen: dict = {}

    async def fake_generate(prompt, **kw):
        seen.update(kw)
        return MagicMock(text="## Run this\n```bash\npct start 111\n```", success=True)

    with patch("app.model_router.generate", new=fake_generate):
        await sr.draft_runbook({"node_key": "T1"}, "brief")
    system = seen["system"]
    assert "No heredocs" in system and "tee" in system and "its OWN shell" in system
    seen.clear()
    with patch("app.model_router.generate", new=fake_generate):
        await sr.draft_runbook({"node_key": "T1"}, "brief", for_channel=False)
    assert "No heredocs" not in seen["system"]


# ── §17.1189 — the engine reads its own plan for the allow-list ───────────

@pytest.mark.parametrize("cmd,want", [
    ("qm set 106 --scsi0 local-lvm:40", "qm set"),
    ("pct set 111 -nameserver 192.168.1.1", "pct set"),
    ("qm resize 106 scsi0 40G", "qm resize"),
    ("pvesm free local-lvm:vm-106-disk-1", "pvesm free"),
    ("apt install -y dkms", "apt install"),
    ("sudo -n systemctl enable palworld", "systemctl enable"),
    ("tee -a /etc/caddy/Caddyfile", "tee -a /etc/caddy/"),   # a bare `tee` would allow writing anywhere
    ("tee /etc/hosts", "tee /etc/"),
    ("mkdir -p /opt/pal/data", "mkdir -p /opt/pal/"),
    ("modprobe nvidia", "modprobe nvidia"),
    ("tee -a", ""),                                          # nothing bounds it: not a prefix, the whole machine
    ("tee report.txt", ""),                                  # a relative path cannot be bounded either
    ("chmod +x /tmp/NVIDIA.run", "chmod +x /tmp/"),         # `+x` is a mode, not a flag, and not the target
    ("chmod 700 /root/.ssh", "chmod 700 /root/"),
    ("cp /etc/network/interfaces /etc/network/interfaces.bak", "cp /etc/network/"),
    ("qm start 106", "qm start"),                            # a digit is not a subcommand
])
def test_prefix_for_is_the_narrowest_entry_that_would_pass(cmd, want):
    assert sr.prefix_for(cmd) == want


def test_a_plan_names_its_own_write_prefixes_and_nothing_else():
    """The `runner_writes` recipe asked the operator to guess this list from a
    generic Proxmox example. Prose is full of inline literals ("Done when `pct
    status 111` reports `running`") — only what the engine's own write test
    calls a write becomes a prefix."""
    nodes = [
        {"node_key": "A1", "title": "Resize the disk",
         "description": "Run `qm resize 106 scsi0 40G`. Done when `qm config 106` reports scsi0 40G and the guest is `running`."},
        {"node_key": "A2", "title": "Point DNS at the router",
         "description": "Run `pct set 111 -nameserver 192.168.1.1`; check with `pct config 111`."},
        {"node_key": "A3", "title": "Grow the other disk", "description": "Run `qm resize 110 scsi0 80G`."},
        {"node_key": "A4", "title": "Reboot the host", "description": "Run `reboot` on the host."},
    ]
    got = sr.prefixes_for_nodes(nodes)
    allowed = [(p["prefix"], p["steps"]) for p in got if p["prefix"]]
    assert ("qm resize", ["A1", "A3"]) in allowed
    assert ("pct set", ["A2"]) in allowed
    assert allowed[0][0] == "qm resize"                       # most widely needed first
    assert not any(p["prefix"] in ("running", "qm config", "pct config") for p in got), got
    never = [p for p in got if not p["prefix"]]
    assert never and never[0]["why"].startswith("never allowed: host power"), never
    assert never[0]["steps"] == ["A4"]


@pytest.mark.asyncio
async def test_write_prefixes_for_job_reads_only_the_steps_still_to_do():
    rows = [{"node_key": "P1", "title": "set it", "description": "Run `qm set 100 --ostype l26`.",
             "prompt_template": "", "tool": "LLM", "node_type": "task"}]
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(mappings=lambda: MagicMock(all=lambda: rows)))
    got = await sr.write_prefixes_for_job(db, "job-1")
    assert [p["prefix"] for p in got] == ["qm set"]
    sql = str(db.execute.await_args[0][0])
    assert "status IN ('pending', 'running', 'failed')" in sql, sql


@pytest.mark.parametrize("cmd,prefixes", [
    # the gate judges PER SEGMENT, and the runbook prompt asks for idempotent
    # one-liners — so it is the FIX that needs allowing, not the check
    ("pct status 111 | grep -q running || pct start 111", ["pct start"]),
    ("qm config 106 | grep -q agent || qm set 106 --agent enabled=1", ["qm set"]),
    ("printf '%s\\n' 'blacklist nouveau' | tee /etc/modprobe.d/bl.conf", ["tee /etc/modprobe.d/"]),
    ("pct config 105 | grep net0", []),                      # read-only throughout
    ("qm stop 100 && qm set 100 --ostype l26", ["qm stop", "qm set"]),
])
def test_prefixes_are_derived_per_segment(cmd, prefixes):
    assert sr.prefixes_for_command(cmd)[0] == prefixes


def test_a_command_on_the_denylist_is_reported_as_never_allowed():
    assert sr.prefixes_for_command("echo ready && reboot") == ([], ["host power — do that by hand"])


def test_the_channel_rules_name_the_forms_the_gate_can_read():
    """Each rule here exists because the real plan lost a step to its absence:
    a heredoc, a `$(…)`, a `>` redirect, a `cd` the next line relied on, and an
    `if … then … fi` whose `then`-part no gate can read."""
    for phrase in ("its OWN shell", "No heredocs", "tee", "if … then … fi", "guard chain", "read-only command"):
        assert phrase in sr.CHANNEL_RULES, phrase


# ── §17.1194 — a refused prefix is a permission request ──────────────────

@pytest.mark.asyncio
async def test_a_refused_prefix_is_remembered_so_it_can_be_offered():
    """The allow-list the engine recommends is read off the PLAN's words; what
    runs is the drafted RUNBOOK, and it reaches for more. Live: ADD65's text
    named `qm agent 106 ping` while its runbook needed `qm start 106`, so the
    run stopped on a prefix the recommendation could not have known about."""
    db = AsyncMock()
    frame = {"node_key": "ADD65", "refused": [
        {"command": "qm status 106 | grep -q running || qm start 106",
         "why": "not on the write-allow list: qm start 106"},
        {"command": "for i in $(seq 1 12); do …", "why": "substitution/heredoc"},
    ]}
    got = await sr.record_wanted_prefixes(db, "job-1", frame)
    assert got == ["qm start"], got                       # the heredoc one is not a permission question
    sql = " ".join(str(db.execute.await_args[0][0]).split())
    assert "wanted_prefixes" in sql and "UPDATE jobs" in sql, sql
    assert json.loads(db.execute.await_args[0][1]["add"]) == ["qm start"]


@pytest.mark.asyncio
async def test_nothing_is_recorded_when_nothing_was_refused_for_permission():
    db = AsyncMock()
    assert await sr.record_wanted_prefixes(db, "j", {"refused": []}) == []
    assert await sr.record_wanted_prefixes(
        db, "j", {"refused": [{"command": "reboot", "why": "host power — do that by hand"}]}) == []
    db.execute.assert_not_awaited()


def test_the_pause_on_screen_is_read_for_wanted_prefixes_too():
    """§17.1195 — reading only what was RECORDED sends an operator to copy an
    install line missing the very prefix the card in front of them is refusing
    (a run parked before the recording existed, or by an older build)."""
    frame = {"refused": [
        {"command": "qm status 106 | grep -q running || qm start 106",
         "why": "not on the write-allow list: qm start 106"},
        {"command": "for i in $(seq 1 12); do …", "why": "substitution/heredoc"},
        {"command": "reboot", "why": "host power — do that by hand"},
    ]}
    assert sr.wanted_prefixes_in(frame) == ["qm start"]
    assert sr.wanted_prefixes_in({}) == [] and sr.wanted_prefixes_in({"refused": []}) == []


# ── §17.1196 — the engine redrafts its own unrunnable block ──────────────

SHAPE = {"refused": [{"command": "for i in $(seq 1 12); do qm agent 106 ping; done",
                      "why": "substitution/heredoc"}]}
PERMISSION = {"refused": [{"command": "qm start 106", "why": "not on the write-allow list: qm start 106"}]}
DENYLIST = {"refused": [{"command": "reboot", "why": "host power — do that by hand"}]}


def test_a_shape_refusal_is_the_engines_own_mistake_and_is_redrafted():
    """`for i in $(seq 1 12)` cannot run however much is allowed, so parking on
    it hands the operator a greyed-out button and no way forward — which is
    exactly what happened: "the runner is active but the run button is greyed
    out?? how do we continue?" """
    note = sr.shape_retry_note(SHAPE)
    assert note, "a shape refusal must produce a correction"
    assert "REFUSED BY THE RUNNER'S GATE" in note
    assert "$(seq 1 12)" in note, "the correction must quote what was actually refused"
    assert "substitution/heredoc" in note
    assert "NEVER build it with" in note or "$(…)" in note


def test_a_permission_refusal_is_the_operators_and_is_not_redrafted_around():
    """Redrafting around a permission is the engine talking itself out of
    asking — the operator decides what their machine may run."""
    assert sr.shape_retry_note(PERMISSION) == ""
    assert sr.shape_retry_note({"refused": SHAPE["refused"] + PERMISSION["refused"]}) == ""


def test_a_denylist_refusal_is_never_redrafted_around():
    assert sr.shape_retry_note(DENYLIST) == ""


def test_a_clean_frame_asks_for_nothing():
    assert sr.shape_retry_note({"refused": []}) == "" and sr.shape_retry_note({}) == ""


@pytest.mark.asyncio
async def test_the_retry_note_reaches_the_second_draft():
    seen = []

    async def fake_generate(prompt, **kw):
        seen.append(prompt)
        return MagicMock(text="## Run this\n```bash\nqm agent 106 ping\n```", success=True)

    with patch("app.model_router.generate", new=fake_generate):
        await sr.draft_runbook({"node_key": "ADD65"}, "brief", retry_note="FIX THIS: no $(…)")
    assert "FIX THIS: no $(…)" in seen[0], "the gate's own refusal never reached the model"


def test_the_executor_redrafts_before_it_asks():
    import inspect
    src = inspect.getsource(ea._pause_for_decision)
    assert "shape_retry_note(frame)" in src
    assert "retry_note=fix" in src
    assert src.index("shape_retry_note(frame)") < src.index("park_awaiting_decision"), \
        "the redraft must happen BEFORE the operator is asked"


# ── §17.1198 — "could not read" is not "the machine is broken" ────────────

PVE_DENIAL = ("ipcc_send_rec[1] failed: Unknown error -1\n"
              "ipcc_send_rec[2] failed: Unknown error -1\n"
              "Unable to load access control list")


@pytest.mark.parametrize("out,hit", [
    (PVE_DENIAL, True),                                  # Proxmox never says "permission"
    ("bash: /etc/pve/x: Permission denied", True),
    ("qm: unable to parse", False),
    ("Configuration file 'nodes/pve/qemu-server/106.conf' does not exist", False),
    ("", False),
])
def test_a_read_that_could_not_read_is_recognised(out, hit):
    assert bool(sr._NEEDS_ROOT_RE.search(out)) is hit


@pytest.mark.asyncio
async def test_a_failed_read_is_reported_as_privilege_not_as_a_broken_step():
    """Live: `qm start 106` ran as root and succeeded, then `qm agent 106 ping`
    ran unprivileged (read-only commands deliberately get no write grant),
    failed, and the step was marked failed as though the machine were broken."""
    waiting = {"kind": "run", "runbook": "## Run this\n```\nqm start 106\nqm agent 106 ping\n```",
               "commands": ["qm start 106", "qm agent 106 ping"], "verify": [], "refused": [], "inputs": []}
    executed = [
        {"command": "qm start 106", "output": "", "exit": 0, "ok": True, "approval_id": "a", "refused": False},
        {"command": "qm agent 106 ping", "output": PVE_DENIAL, "exit": 255, "ok": False, "approval_id": "b", "refused": False},
    ]
    db = AsyncMock()
    claim = MagicMock(); claim.rowcount = 1
    db.execute = AsyncMock(return_value=claim)
    with patch.object(sr, "channel", new=AsyncMock(return_value=(_spec(), {"allow": ["qm start"], "sudo": True}))), \
         patch("app.modules.assist_supervised.run_block", new=AsyncMock(return_value=executed)), \
         patch.object(sr, "record_needs_root", new=AsyncMock(return_value=["qm agent"])) as rec:
        out = await sr.resolve_run(db, "j", "ADD65", "run", waiting)
    assert out["outcome"] == "failed"
    assert "could not read on that machine" in out["reason"]
    assert "unprivileged for READ commands" in out["reason"]
    assert "Settings → Machines" in out["reason"]
    assert "exited 255" not in out["reason"], "the raw exit code is not the explanation"
    rec.assert_awaited_once()
    assert rec.await_args[0][2] == ["qm agent 106 ping"]


@pytest.mark.asyncio
async def test_the_read_grant_is_recorded_as_a_prefix():
    db = AsyncMock()
    got = await sr.record_needs_root(db, "job-1", ["qm agent 106 ping", "qm agent 110 ping", "pct config 111"])
    assert got == ["qm agent", "pct config"]            # de-duplicated, narrowed to prefixes
    sql = " ".join(str(db.execute.await_args[0][0]).split())
    assert "needs_root_prefixes" in sql
