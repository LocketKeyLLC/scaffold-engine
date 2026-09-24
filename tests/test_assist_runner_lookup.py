"""§17.1150 — the connected local runner carries the engine's own read-only look-ups."""
import inspect
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_runner_lookup as rl

GUIDE = """📍 On: the Proxmox host shell (root@pve)

## 👉 Do this next

**Run this now:**

```bash
cat /etc/pve/firewall/host.fw
pve-firewall status
iptables -S PVEFW-HOST-IN
```

Then tell me what it shows.

## Run this

1. Later, the real change:

```bash
pvesh create /nodes/$(hostname)/firewall/groups --group media
```
"""


def test_first_block_is_the_run_this_now_one_and_read_only_lines_are_the_lookup():
    blk = rl.first_lookup_block(GUIDE)
    assert blk.startswith("cat /etc/pve/firewall/host.fw") and "pvesh create" not in blk
    runnable, refused = rl.lookup_lines(blk)
    assert runnable == ["cat /etc/pve/firewall/host.fw", "pve-firewall status", "iptables -S PVEFW-HOST-IN"] and refused == []
    assert rl.is_lookup(GUIDE) == runnable
    # all-or-nothing: a read-only line after a refused one may only mean something once the refused one ran
    mixed = GUIDE.replace("pve-firewall status\n", "systemctl restart pve-firewall\npve-firewall status\n")
    assert rl.is_lookup(mixed) is None
    # an older helper on the target refuses a form the engine now allows → the note says how to refresh it
    note = rl.render_note("pve-runner", [{"id": "L1", "command": "dpkg -l x", "ok": True, "chars": 3}], "== L1 ==\n(refused by the local runner: mutation verb dpkg)\n")
    assert "older than the engine's read-only rules" in note
    # a block that writes anywhere is NOT a look-up — it stays the operator's
    doing = "**Run this now:**\n\n```bash\nls /etc/pve/firewall/groups/\nsystemctl restart pve-firewall\n```\nThen tell me."
    assert rl.is_lookup(doing) is None
    assert rl.lookup_lines("ls /x\nrm -rf /y\n# comment\n\n$ cat /z")[1] == ["rm -rf /y"]
    assert rl.lookup_lines("cd /tmp\nexport A=1\ncat /etc/hostname")[0] == ["cat /etc/hostname"]
    assert rl.is_lookup("no fence here") is None and rl.is_lookup("") is None
    # a fix answer without the header still counts by its first fence
    assert rl.is_lookup("Try this:\n```bash\nls /etc/pve/firewall/groups/\n```") == ["ls /etc/pve/firewall/groups/"]


def test_record_and_note_carry_each_command_with_its_output():
    executed = [{"id": "L1", "command": "cat /etc/hostname", "ok": True, "chars": 4},
                {"id": "L2", "command": "pve-firewall status", "ok": True, "chars": 0}]
    pasted = "== L1 ==\npve\n== L2 ==\n\n"
    rec = rl.record_text("pve-runner", executed, pasted)
    assert rec.startswith("[local-runner] ran the walkthrough's read-only look-up through your local runner (pve-runner)")
    assert "ON THE TARGET MACHINE itself" in rec.splitlines()[0]     # the model must not read the runner as a sandbox
    assert "$ cat /etc/hostname\npve" in rec and "$ pve-firewall status\n(no output)" in rec
    note = rl.render_note("pve-runner", executed, pasted)
    assert note.startswith("🔁 Your local runner is connected, so I ran that look-up myself (2 read-only commands through pve-runner)")
    assert "$ cat /etc/hostname" in note


def test_reply_tail_keeps_the_last_guide_or_fix():
    t = rl.ReplyTail()
    t.feed("assist_guide_delta", {"text": "## 👉 Do this next\n```bash\ncat /a\n```"}); t.feed("assist_guide_done", {})
    assert "cat /a" in t.text()
    t.feed("assist_answer", {"kind": "ask", "text": "a question"})           # not a reply that carries commands
    assert "cat /a" in t.text()
    t.feed("assist_answer", {"kind": "fix", "text": "```bash\nls /b\n```"})
    assert t.text() == "```bash\nls /b\n```"
    t.feed("assist_guide_delta", {"text": "new guide"}); t.feed("assist_guide_done", {})
    assert t.text() == "new guide"


@pytest.mark.asyncio
async def test_run_turn_runs_the_lookup_and_reenters_with_the_output_bounded(monkeypatch):
    """The wiring: a turn ending on a read-only block → the runner runs it →
    the output re-enters as the operator's paste → at most MAX_AUTO_ROUNDS."""
    from app.modules import assist_turn, assist_local_runner as lr
    seen = []
    async def _inner(*, session_id, message, command, node_key, history, db, handled, capture_node_key=None):
        seen.append((command, (message or "")[:60], node_key, capture_node_key))
        handled["v"] = "guide"
        yield ("assist_guide_delta", {"text": "**Run this now:**\n```bash\ncat /etc/hostname\n```\nThen tell me what it shows."})
        yield ("assist_guide_done", {"status": "ready", "node_key": "T6"})
    spec = MagicMock(); spec.name = "pve-runner"
    monkeypatch.setattr(assist_turn, "_run_turn_inner", _inner)
    monkeypatch.setattr(lr, "runner_spec", AsyncMock(return_value=spec))
    from app.modules import engine_setup as es
    monkeypatch.setattr(es, "ensure_helper_refresh_step", AsyncMock(return_value=None))
    monkeypatch.setattr(es, "recipe_context", AsyncMock(return_value={"target_host": "pve", "target_ip": "192.168.1.156", "target_user": "root"}))
    monkeypatch.setattr(rl, "run_lookup", AsyncMock(return_value=("== L1 ==\npve\n", [{"id": "L1", "command": "cat /etc/hostname", "ok": True, "chars": 3}])))
    ev = [e async for e in assist_turn.run_turn(session_id="s", message=None, command="guide", node_key="T6", history=[], db=MagicMock())]
    names = [e[0] for e in ev]
    # §17.1152 — the inner loop replays the SAME block every time, so the repeat guard ends
    # the turn after ONE round (a model repeating itself must not spin the runner)
    assert names.count("assist_guide_done") == 2                           # guide, then one re-entry
    assert names[-1] == "assist_turn_done" and ev[-1][1]["handled"] == "guide+lookup"
    assert "_record" not in names                                          # the private frame never reaches the client
    notes = [e for e in ev if e[0] == "assist_answer" and e[1].get("kind") == "note"]
    assert len(notes) == 1 and notes[0][1]["text"].startswith("🔁 Your local runner is connected")
    assert seen[0] == ("guide", "", "T6", None)
    # §17.1166 — routing still resolves from the pointer (node_key None), but the
    # look-up turn is FILED against the step the reply was written for ("T6"),
    # not with a NULL node_key as it was live.
    assert seen[1][0] == "message" and seen[1][1].startswith("[local-runner] ran the walkthrough")
    assert seen[1][2] is None          # routing still resolves from the pointer (a repair commit re-points, §17.1152)
    assert seen[1][3] == "T6"          # §17.1166 — but the look-up turn is FILED against the step it answered
    # a DIFFERENT block each round runs up to MAX_AUTO_ROUNDS
    seen.clear(); n = {"i": 0}
    async def _inner3(*, session_id, message, command, node_key, history, db, handled, capture_node_key=None):
        n["i"] += 1; seen.append(command); handled["v"] = "guide"
        yield ("assist_guide_delta", {"text": f"**Run this now:**\n```bash\ncat /etc/file{n['i']}\n```"})
        yield ("assist_guide_done", {"status": "ready", "node_key": "T6"})
    monkeypatch.setattr(assist_turn, "_run_turn_inner", _inner3)
    ev = [e async for e in assist_turn.run_turn(session_id="s", message=None, command="guide", node_key="T6", history=[], db=MagicMock())]
    assert ev[-1][1]["handled"] == "guide+lookup+lookup" and len(seen) == 1 + rl.MAX_AUTO_ROUNDS
    # no runner → no round trip at all
    seen.clear(); monkeypatch.setattr(lr, "runner_spec", AsyncMock(return_value=None))
    ev = [e async for e in assist_turn.run_turn(session_id="s", message=None, command="guide", node_key="T6", history=[], db=MagicMock())]
    assert len(seen) == 1 and ev[-1][1]["handled"] == "guide"
    # a mutating block → the operator's hands, no round trip
    async def _inner2(*, session_id, message, command, node_key, history, db, handled, capture_node_key=None):
        handled["v"] = "fix"
        yield ("assist_answer", {"kind": "fix", "text": "```bash\nsystemctl restart pve-firewall\n```"})
    monkeypatch.setattr(assist_turn, "_run_turn_inner", _inner2); monkeypatch.setattr(lr, "runner_spec", AsyncMock(return_value=spec))
    ev = [e async for e in assist_turn.run_turn(session_id="s", message="x", command="message", node_key="T6", history=[], db=MagicMock())]
    assert ev[-1][1]["handled"] == "fix" and not [e for e in ev if e[0] == "assist_turn_status" and "runner" in e[1]["text"]]


def test_run_turn_is_the_single_wiring_point():
    from app.modules import assist_turn
    src = inspect.getsource(assist_turn.run_turn)
    assert "_auto_lookup(" in src and "MAX_AUTO_ROUNDS" in src and 'cmd, rounds = record, "message"' in src
    assert src.index("_run_turn_inner(") < src.index("_auto_lookup(")


# ---------------------------------------------------------------------------
# §17.1152 — the block must be for the runner's machine; a repeated look-up ends the loop.
# ---------------------------------------------------------------------------

def test_block_location_and_runner_host_rule():
    console = "## 👉 Do this next\n\n**Open the Proxmox web UI console for VM 110 and type this:**\n\n```\nip a\n```\n\n📍 On: Proxmox web UI console for VM 110 (ai-vm) — you're leaving the root@pve shell"
    assert rl.first_lookup_block(console) == "ip a"
    where = rl.block_location(console)
    assert "console" in where.lower()
    assert rl.location_is_runner_host(where, host="pve", ip="192.168.1.156", user="root") is False
    shell = "📍 On: the Proxmox host shell (root@pve)\n\n**Run this now:**\n```bash\nqm status 110\n```"
    assert rl.location_is_runner_host(rl.block_location(shell), host="pve", ip="192.168.1.156", user="root") is True
    assert rl.location_is_runner_host(None, host="pve", ip=None, user=None) is True                       # no line → the shell
    assert rl.location_is_runner_host("aedefruscio@192.168.1.127 (inside VM 110)", host="pve", ip="192.168.1.156", user="root") is False
    assert rl.location_is_runner_host("root@192.168.1.156", host="pve", ip="192.168.1.156", user="root") is True
    assert rl.location_is_runner_host("the container (pct exec 111)", host="pve", ip=None, user=None) is False
    # the governing line is the nearest one BEFORE the first block
    two = "📍 On: root@pve\n```bash\nqm status 110\n```\n📍 On: VM 110 console\n```\nip a\n```"
    assert rl.block_location(two) == "root@pve"


@pytest.mark.asyncio
async def test_lookup_skips_console_blocks_and_repeats(monkeypatch):
    from app.modules import assist_turn, assist_local_runner as lr, engine_setup as es
    spec = MagicMock(); spec.name = "pve-runner"
    monkeypatch.setattr(lr, "runner_spec", AsyncMock(return_value=spec))
    monkeypatch.setattr(es, "ensure_helper_refresh_step", AsyncMock(return_value=None))
    monkeypatch.setattr(es, "recipe_context", AsyncMock(return_value={"target_host": "pve", "target_ip": "192.168.1.156", "target_user": "root"}))
    monkeypatch.setattr(rl, "run_lookup", AsyncMock(return_value=("== L1 ==\nx\n", [{"id": "L1", "command": "ip a", "ok": True, "chars": 1}])))
    console = "📍 On: Proxmox web UI console for VM 110\n\n**Run this now:**\n```\nip a\n```"
    ev = [e async for e in assist_turn._auto_lookup("s", console, MagicMock(), set())]
    assert ev == []                                                     # not the runner's machine → nothing runs
    shell = "📍 On: the Proxmox host shell (root@pve)\n\n**Run this now:**\n```bash\nip a\n```"
    ran = set()
    ev = [e async for e in assist_turn._auto_lookup("s", shell, MagicMock(), ran)]
    assert any(e[0] == "_record" for e in ev) and ran == {("ip a",)}
    ev = [e async for e in assist_turn._auto_lookup("s", shell, MagicMock(), ran)]
    assert ev == []                                                     # the same look-up again this turn → the loop ends
    src = inspect.getsource(assist_turn.run_turn)
    assert "ran: set = set()" in src and "_auto_lookup(session_id, tail.text(), db, ran)" in src


def test_verify_state_runs_every_batch_through_the_runner_and_repair_commit_repoints():
    from app.modules import assist_turn, engine_setup as es
    from app.routers import assist as r
    src = inspect.getsource(assist_turn._start_state_check)
    blk = src[src.index("§17.1152 — EVERY batch"):src.index("falling back to the paste")]
    assert "while True:" in blk and "resolve_state_check(" in blk and "get_pending_state_check(" in blk and "run_probes(_spec, _pend[\"probes\"]" in blk
    assert "_batches >= 12" in blk                                      # bounded
    rs = inspect.getsource(r.assist_submit)
    assert rs.index("submit_step(") < rs.index("repoint_after_repair(") < rs.index('result["success_verdict"] = verdict')
    assert '_recipe_verdict is not None and result.get("status") == "committed"' in rs
    es_src = inspect.getsource(es.repoint_after_repair)
    assert ":nk = ANY(d.depends_on)" in es_src and "NOT LIKE :m2" in es_src and "_present(" in es_src
    assert "RECIPE_STEP_MARK" not in es_src.split("SELECT d.node_key")[1].split("LIMIT 1")[0]   # the VERIFY step is a valid target


# ---------------------------------------------------------------------------
# §17.1156 — every leading read-only block runs, in order; a write ends the scan; rollback is never run.
# ---------------------------------------------------------------------------

def test_leading_read_only_blocks_all_run_and_a_write_ends_the_scan():
    two = ("## 👉 Do this next\n\n**Run this now:**\n```bash\ncat /etc/os-release | head -1\n```\n\n## Run this\n"
           "1. Reveal:\n```bash\ncat /etc/os-release | head -1\n```\n"            # the same block repeated → once
           "2. Then look at the firewall:\n```bash\npve-firewall status\nss -tlnp | grep 8790\n```\n"
           "3. Now change it:\n```bash\npvesh create /nodes/x/firewall/rules --type in --action ACCEPT\n```\n"
           "4. And confirm:\n```bash\npve-firewall status\n```\n"                 # after the write → never run now
           "## Rollback\n```bash\nls /etc/pve/firewall\n```\n")                    # read-only but under Rollback → skipped
    assert rl.lookup_blocks(two) == ["cat /etc/os-release | head -1", "pve-firewall status\nss -tlnp | grep 8790"]
    assert rl.is_lookup(two) == ["cat /etc/os-release | head -1", "pve-firewall status", "ss -tlnp | grep 8790"]
    # a reply whose FIRST block writes is not a look-up at all
    assert rl.is_lookup("**Run this now:**\n```bash\nsystemctl restart x\n```\n```bash\nls /\n```") is None
    # all read-only → every block, bounded
    many = "**Run this now:**\n" + "".join(f"```bash\ncat /f{i}\n```\n" for i in range(10))
    assert len(rl.lookup_blocks(many)) == rl.MAX_LOOKUP_BLOCKS
    assert rl.is_lookup(GUIDE) == ["cat /etc/pve/firewall/host.fw", "pve-firewall status", "iptables -S PVEFW-HOST-IN"]   # unchanged: its 2nd block writes


# ---------------------------------------------------------------------------
# §17.1158 — a look-up the runner already ran is redundant discovery: the fix is regenerated with the answer.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_recent_lookups_parses_runner_records_newest_first_and_dedupes():
    import datetime as dt
    t1, t2 = dt.datetime(2026, 9, 20, 22, 59), dt.datetime(2026, 9, 20, 23, 12)
    rows = [
        {"content": "[local-runner] ran the walkthrough's read-only look-up through your local runner (pve-runner) — these commands ran ON THE TARGET MACHINE itself:\n$ qm guest cmd 110 network-get-interfaces\nNo QEMU guest agent configured\n$ qm status 110\nstatus: running", "created_at": t2},
        {"content": "[local-runner] the engine checked whether VM 110 (ai-vm) is reachable (2 read-only commands on the host):\n$ qm status 110\nstatus: stopped\n$ ping -c 1 -W 2 192.168.1.127 2>&1 | tail -2\n1 packets transmitted, 0 received", "created_at": t1},
    ]
    db = MagicMock(); db.execute = AsyncMock(return_value=MagicMock(mappings=lambda: MagicMock(all=lambda: rows)))
    led = await rl.recent_lookups(db, "s")
    cmds = [e["command"] for e in led]
    assert cmds == ["qm guest cmd 110 network-get-interfaces", "qm status 110", "ping -c 1 -W 2 192.168.1.127 2>&1 | tail -2"]
    assert led[1]["output"] == "status: running" and led[1]["at"] == t2          # the newest record wins
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    assert await rl.recent_lookups(db, "s") == []                                 # fail-soft


def test_find_repeated_lookups_names_when_it_ran_and_what_it_printed():
    import datetime as dt
    ledger = [{"command": "qm guest cmd 110 network-get-interfaces", "output": "No QEMU guest agent configured", "at": dt.datetime(2026, 9, 20, 22, 59)}]
    draft = "## 👉 Do this next\n\n**Run this now:**\n```bash\nqm guest cmd 110 network-get-interfaces\n```\nThen tell me what it prints."
    hits = rl.find_repeated_lookups(draft, ledger)
    assert len(hits) == 1 and hits[0]["command"] == "qm guest cmd 110 network-get-interfaces"
    assert "22:59 UTC" in hits[0]["known"] and "No QEMU guest agent configured" in hits[0]["known"]
    assert rl.find_repeated_lookups("```bash\nqm status 110\n```", ledger) == []          # not in the ledger
    assert rl.find_repeated_lookups(draft, None) == [] and rl.find_repeated_lookups("", ledger) == []


def test_fix_gate_folds_repeated_lookups_into_redundancy_and_the_ledger_is_threaded():
    from app.modules import assist_guide, assist_agent
    src = inspect.getsource(assist_guide.generate_fix)
    assert "runner_ledger: Optional[list[dict]] = None" in src
    assert "find_repeated_lookups(draft, runner_ledger)" in src
    assert src.index("find_redundant_discovery(draft, _known_state)") < src.index("find_repeated_lookups(draft, runner_ledger)")
    assert "engine itself through the local runner" in src                     # the directive says so
    asrc = inspect.getsource(assist_agent.run_step_fix)
    assert "_recent_lookups(db, session_id)" in asrc and "runner_ledger=_runner_ledger" in asrc


# ---------------------------------------------------------------------------
# §17.1166 — a refusal is not an answer; a walkthrough gets the answers it has.
# ---------------------------------------------------------------------------

def test_a_refusal_or_transport_failure_is_not_an_answer():
    assert rl.is_answer("status: stopped")
    assert rl.is_answer("(no output)")            # it RAN and matched nothing
    assert not rl.is_answer("(refused by the local runner: mutation verb apt)")
    assert not rl.is_answer("(runner error: connection refused)")
    assert not rl.is_answer("(timed out after 20s)")


@pytest.mark.asyncio
async def test_ledger_skips_refusals_so_the_gate_cannot_argue_against_the_right_fix():
    """The live ADD65 shape (2026-09-22 23:02–23:04): the runner REFUSED
    `sudo apt install -y qemu-guest-agent` (correctly — it mutates), the old
    ledger filed it as known, and the redundancy gate then flagged the one
    correct fix ("open the VM console and install the agent") as asking to
    re-run something already answered."""
    import datetime as dt
    t = dt.datetime(2026, 9, 22, 23, 2)
    rows = [{"content": ("[local-runner] ran the walkthrough's read-only look-up:\n"
                         "$ qm config 106\nagent: 1\nboot: order=ide2\n"
                         "$ sudo apt install -y qemu-guest-agent\n(refused by the local runner: mutation verb apt)\n"
                         "$ qm agent 106 ping\nQEMU guest agent is not running\n"),
             "created_at": t}]
    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(mappings=lambda: MagicMock(all=lambda: rows)))
    led = await rl.recent_lookups(db, "s")
    cmds = [e["command"] for e in led]
    assert "qm config 106" in cmds and "qm agent 106 ping" in cmds
    assert "sudo apt install -y qemu-guest-agent" not in cmds        # never ran → nothing known
    fix = ("**Open the Proxmox web UI console for VM 106 and type this:**\n"
           "```bash\nsudo apt install -y qemu-guest-agent\n```\n")
    assert rl.find_repeated_lookups(fix, led) == []


def test_runner_ledger_block_hands_the_walkthrough_the_answers():
    import datetime as dt
    led = [{"command": "qm status 106", "output": "status: stopped",
            "at": dt.datetime(2026, 9, 22, 22, 53), "by": "runner"},
           {"command": "qm agent 106 ping", "output": "QEMU guest agent is not running",
            "at": dt.datetime(2026, 9, 22, 23, 2), "by": "operator"}]
    block = rl.runner_ledger_block(led)
    assert "ALREADY RUN THIS SESSION" in block
    assert "Do NOT ask the operator to run any of them again" in block
    assert "$ qm status 106" in block and "status: stopped" in block
    assert "the engine ran it on the target machine, 22:53 UTC" in block
    assert "the operator ran it, 23:02 UTC" in block
    assert rl.runner_ledger_block([]) == "" and rl.runner_ledger_block(None) == ""
    long = [{"command": "cat /x", "output": "y" * 5000, "at": None, "by": "runner"}]
    assert len(rl.runner_ledger_block(long)) < 1200                  # per-output cap


def test_the_guide_path_consults_the_ledger_at_both_ends():
    """§17.1158 wired the ledger into generate_fix ONLY and recorded 'guides do
    not consult the ledger'. Live ADD65: the runner printed `status: stopped`
    at 22:53:19 and the guide asked for `qm status 106` 34 seconds later."""
    from app.modules import assist_guide
    builder = inspect.getsource(assist_guide._build_guide_user_prompt)
    assert "runner_ledger" in builder and "runner_ledger_block(runner_ledger)" in builder
    for fn in (assist_guide.generate_guidance, assist_guide.generate_guidance_stream,
               assist_guide.ensure_guidance):
        assert "runner_ledger" in inspect.signature(fn).parameters, fn.__name__
    # the streaming path (the SPA's route) fetches it itself — no caller threading
    stream = inspect.getsource(assist_guide.generate_guidance_stream)
    assert "recent_lookups as _rlk" in stream and "runner_ledger = await _rlk(db, session_id)" in stream
    assert "runner_ledger=runner_ledger" in stream
    # and the visible backstop when a draft asks anyway
    warn = inspect.getsource(assist_guide.guide_integrity_warning)
    assert "find_repeated_lookups(text_out, runner_ledger)" in warn
    assert "already on file" in warn


def test_guide_integrity_warning_flags_an_ask_the_engine_already_answered():
    import datetime as dt
    from app.modules.assist_guide import guide_integrity_warning
    led = [{"command": "qm status 106", "output": "status: stopped",
            "at": dt.datetime(2026, 9, 22, 22, 53), "by": "runner"}]
    draft = "## 👉 Do this next\n\n**Run this now:**\n```bash\nqm status 106\n```\nthen tell me what it shows."
    warn = guide_integrity_warning(draft, "", "", led)
    assert "already on file" in warn and "qm status 106" in warn
    assert guide_integrity_warning(draft, "", "", []) == ""        # nothing on file → no flag


@pytest.mark.asyncio
async def test_a_runner_lookup_turn_is_filed_against_the_step_not_NULL():
    """§17.1166 — the loop re-entered with `node_key = None`, so every
    `[local-runner]` look-up turn was written with a NULL node_key (live
    ADD65: turns 3100/3104/3112/3114/3119/3121). That put the engine's own
    findings outside the step's history, outside the per-step 'already tried'
    harvest (§17.973), and outside the Follow pane's step filter (§17.1160) —
    the operator could not see, in the pane, the look-up their runner ran."""
    from app.modules import assist_turn
    src = inspect.getsource(assist_turn.run_turn)
    assert "capture_nk = tail.node_key() or await _current_node_key(db, session_id)" in src
    # routing still resolves from the pointer — a repair commit re-points (§17.1152)
    assert "node_key = None" in src and "capture_node_key=capture_nk" in src
    inner = inspect.getsource(assist_turn._run_turn_inner)
    assert "content=text_, node_key=capture_node_key or node_key, db=db," in inner

    db = MagicMock()
    db.execute = AsyncMock(return_value=MagicMock(
        mappings=lambda: MagicMock(first=lambda: {"current_node_key": "ADD65"})))
    assert await assist_turn._current_node_key(db, "s") == "ADD65"
    db.execute = AsyncMock(return_value=MagicMock(
        mappings=lambda: MagicMock(first=lambda: None)))
    assert await assist_turn._current_node_key(db, "s") is None       # no session row
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    assert await assist_turn._current_node_key(db, "s") is None       # fail-soft


def test_the_lookup_is_filed_against_the_step_the_reply_was_written_for():
    """Live 22:51:52: a walkthrough for ADD17 asked for `nvidia-smi`, the
    runner ran it, the output re-entered the loop with NO step, and the next
    reply landed on ADD65 — where it called the engine's own result 'a red
    herring'. The reply's own step beats the session pointer."""
    t = rl.ReplyTail()
    t.feed("assist_guide_delta", {"text": "**Run this now:**\n```bash\nnvidia-smi\n```"})
    t.feed("assist_guide_done", {"node_key": "ADD17"})
    assert t.node_key() == "ADD17" and "nvidia-smi" in t.text()
    # a fix answer stamps its step too
    t2 = rl.ReplyTail()
    t2.feed("assist_answer", {"kind": "fix", "text": "```bash\nqm status 106\n```", "node_key": "ADD65"})
    assert t2.node_key() == "ADD65"
    # an unstamped frame leaves it to the caller's fallback (the session pointer)
    t3 = rl.ReplyTail()
    t3.feed("assist_answer", {"kind": "fix", "text": "x"})
    assert t3.node_key() is None
    # a later stamped reply wins; an unstamped later one does not erase it
    t.feed("assist_answer", {"kind": "fix", "text": "y"})
    assert t.node_key() == "ADD17"


@pytest.mark.asyncio
async def test_the_ledger_is_one_list_newest_first_not_runner_then_operator():
    """§17.1166 — the two queries used to append runner entries then operator
    entries, so `ledger[:N]` (runner_ledger_block) never reached a paste."""
    import datetime as dt
    old_r = dt.datetime(2026, 9, 22, 20, 0)
    new_op = dt.datetime(2026, 9, 22, 23, 0)
    runner_rows = [{"content": "[local-runner] look-up:\n$ qm config 106\nagent: 1\n", "created_at": old_r}]
    paste_rows = [{"content": "root@pve:~# qm status 106\nstatus: running\n", "created_at": new_op}]
    calls = {"n": 0}

    async def _exec(*a, **k):
        calls["n"] += 1
        rows = runner_rows if calls["n"] == 1 else paste_rows
        return MagicMock(mappings=lambda: MagicMock(all=lambda: rows))

    db = MagicMock(); db.execute = AsyncMock(side_effect=_exec)
    led = await rl.recent_lookups(db, "s")
    assert [e["command"] for e in led] == ["qm status 106", "qm config 106"]
    assert led[0]["by"] == "operator" and led[1]["by"] == "runner"


# ---------------------------------------------------------------------------
# §17.1168 — the row cap, not the time window, was the binding constraint.
# ---------------------------------------------------------------------------

def test_the_row_cap_does_not_bound_what_the_time_window_covers():
    """MEASURED on the operator's real session: operator pastes reached 46 in a
    180-minute window (12 such windows), so `LIMIT 40` dropped rows the window
    still covered — during fast iteration, when a repeat is most likely. The
    WINDOW is deliberately unchanged: 8 of 10 repeats fall inside it, and the 2
    outside are 25 h and 48 h apart, where re-running is correct."""
    import inspect as _i
    sig = _i.signature(rl.recent_lookups).parameters
    assert sig["minutes"].default == 180            # time still bounds staleness
    assert sig["limit_turns"].default >= 120        # rows no longer bound recall
    assert rl._LEDGER_TURNS >= 120


@pytest.mark.asyncio
async def test_a_busy_window_keeps_its_oldest_rows(monkeypatch):
    """46 pastes in one window — the shape that was truncated at 40."""
    import datetime as dt
    base = dt.datetime(2026, 9, 22, 20, 0)
    prows = [{"content": f"root@pve:~# qm config 1{i:02d}\nagent: 1\n",
              "created_at": base + dt.timedelta(minutes=i)} for i in range(46)]
    captured = {}

    async def _exec(q, params=None):
        sql = str(q)
        # NOTE order matters: the paste query says `content NOT LIKE
        # '[local-runner]%'`, which CONTAINS the runner query's predicate, so
        # matching the runner branch first swallowed the paste query entirely.
        if "kind IN ('guide', 'fix')" in sql:
            return MagicMock(scalars=lambda: MagicMock(all=lambda: []))
        if "NOT LIKE '[local-runner]%'" in sql:
            captured["paste_n"] = (params or {}).get("n")
            return MagicMock(mappings=lambda: MagicMock(all=lambda: prows))
        captured["runner_n"] = (params or {}).get("n")
        return MagicMock(mappings=lambda: MagicMock(all=lambda: []))

    db = MagicMock(); db.execute = AsyncMock(side_effect=_exec)
    led = await rl.recent_lookups(db, "s")
    assert captured["paste_n"] >= 120 and captured["runner_n"] >= 120
    assert len(led) == 46                      # all 46 distinct commands survive
    assert led[0]["command"] == "qm config 145"   # newest first


def test_the_prompt_block_is_budgeted_so_a_wide_ledger_cannot_eat_the_guide():
    import datetime as dt
    big = [{"command": f"cat /very/long/path/number/{i}", "output": "x" * 900,
            "at": dt.datetime(2026, 9, 22, 22, 0), "by": "runner"} for i in range(40)]
    block = rl.runner_ledger_block(big)
    assert len(block) < 4600                      # budget + the header
    assert "cat /very/long/path/number/0" in block    # the NEWEST are kept
    # one entry alone is never dropped, however long
    one = [{"command": "cat /x", "output": "y" * 9000, "at": None, "by": "runner"}]
    assert "cat /x" in rl.runner_ledger_block(one)


def test_every_decision_branch_in_auto_lookup_is_legible():
    """§17.1170 — a feature that declines to act must say why.

    §17.1169 fixed ONE silent early return (`spec is None`) and I asserted in
    that commit it was the only one. It was not: `if not commands:` and
    `if not executed:` were silent too — and the first is exactly the branch
    that could not be ruled out from logs during the hour it took to find the
    other. This pins the CLASS: every `return` in `_auto_lookup` must either
    log, or hand the caller something it can see (a yielded frame), or be a
    genuine no-op on empty input. A new silent branch fails here."""
    import inspect as _i
    from app.modules import assist_turn
    src = _i.getsource(assist_turn._auto_lookup).splitlines()
    silent: list[str] = []
    for i, line in enumerate(src):
        if line.strip() != "return":
            continue
        # the preceding 6 non-blank, non-comment lines — a multi-line
        # logger.info(...) call reads as silent if only one line is inspected,
        # which is what hid these two from my first enumeration.
        ctx, j = [], i - 1
        while j >= 0 and len(ctx) < 6:
            t = src[j].strip()
            if t and not t.startswith("#"):
                ctx.append(t)
            j -= 1
        window = " ".join(ctx)
        if "logger." in window or "yield" in window:
            continue
        if 'not (reply_text or "").strip()' in window:   # no input, no decision
            continue
        silent.append(f"line {i}: {ctx[0][:70] if ctx else '?'}")
    assert not silent, "silent early return(s) in _auto_lookup — say why it declined:\n" + "\n".join(silent)


def test_the_two_reasons_a_reply_is_not_a_lookup_are_distinguished():
    """`no_block` (nothing runnable was asked for) and `not_read_only` (the
    engine WON'T run it, so the operator must) are different decisions."""
    from app.modules import assist_runner_lookup as _rl
    assert _rl.first_lookup_block("just prose, no block at all") is None
    writing = "**Run this now:**\n\n```bash\napt-get install -y nginx\n```"
    assert _rl.first_lookup_block(writing) is not None      # there IS a block …
    assert _rl.is_lookup(writing) is None                   # … the engine just won't run it
