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
    async def _inner(*, session_id, message, command, node_key, history, db, handled):
        seen.append((command, (message or "")[:60], node_key))
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
    assert seen[0] == ("guide", "", "T6")
    assert seen[1][0] == "message" and seen[1][1].startswith("[local-runner] ran the walkthrough") and seen[1][2] is None
    # a DIFFERENT block each round runs up to MAX_AUTO_ROUNDS
    seen.clear(); n = {"i": 0}
    async def _inner3(*, session_id, message, command, node_key, history, db, handled):
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
    async def _inner2(*, session_id, message, command, node_key, history, db, handled):
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
