"""§17.1159 — a paste is parsed once, deterministically, into what was typed and what was printed."""
import pytest

from app.modules import assist_paste as ap

LIVE = """root@pve:~# pve-firewall status; ss -tlnp | grep 8790
Status: enabled/running
LISTEN 0      2048                       0.0.0.0:8790       0.0.0.0:*    users:(("python",pid=102032,fd=6))
root@pve:~# pvesh create /nodes/$(hostname)/firewall/rules --type in --action ACCEPT --proto tcp --dport 8790 --source 192.168.1.0/24 --enable 1 --comment 'scaffold local runner'
root@pve:~# sleep 3; pve-firewall status
Status: enabled/running
root@pve:~#"""


def test_parse_live_paste_into_pairs_with_host_and_trailing_prompt():
    p = ap.parse_paste(LIVE)
    assert [e.command.split()[0] for e in p.entries] == ["pve-firewall", "pvesh", "sleep"] and len(p.entries) == 3
    assert p.entries[0].output.startswith("Status: enabled/running\nLISTEN") and p.entries[1].output == "" and p.entries[2].output == "Status: enabled/running"
    assert p.hosts == ["pve"] and p.entries[0].user == "root"
    assert p.prompt_seen and p.trailing_prompt and not p.continuation and p.failed == []
    txt = ap.render_pairs(p)
    assert txt.startswith("[1] root@pve$ pve-firewall status; ss -tlnp | grep 8790\n    Status: enabled/running") and "[2] root@pve$ pvesh create" in txt and "(no output)" in txt


def test_errors_are_attributed_to_their_command_and_heredoc_bodies_are_not_output():
    text = ("root@pve:~# ssh aedefruscio@192.168.1.127 nvidia-smi\n"
            "ssh: connect to host 192.168.1.127 port 22: No route to host\n"
            "root@pve:~# cat > /etc/x.conf << 'EOF'\n"
            "error_log = /var/log/x   # 'error' inside a config body is NOT a failure\n"
            "EOF\n"
            "root@pve:~# systemctl is-active x\nactive\nroot@pve:~#")
    p = ap.parse_paste(text)
    assert [e.errored for e in p.entries] == [True, False, False]
    assert p.entries[1].heredoc and p.entries[1].output == ""
    assert [e.command for e in p.failed] == ["ssh aedefruscio@192.168.1.127 nvidia-smi"]
    assert "[wrote a file via heredoc" in ap.render_pairs(p)


def test_exit_code_lines_and_multiline_commands():
    text = "root@pve:~# nc -z -w 2 192.168.1.127 22 2>&1; echo rc=$?\nrc=1\nroot@pve:~# for i in 1 2; do\n> echo $i\n> done\n1\n2\nroot@pve:~#"
    p = ap.parse_paste(text)
    assert p.entries[0].exit_code == 1 and p.entries[0].errored and p.entries[0].output == ""
    assert p.entries[1].command == "for i in 1 2; do echo $i done" and p.entries[1].output == "1\n2" and not p.continuation


def test_shape_guards_hung_prompt_still_running_and_copied_back_block():
    assert "waiting for more input" in ap.shape_guard("root@pve:~# echo 'oops\n> ")
    running = "root@pve:~# apt-get install -y openssh-server\n"
    assert "may still be running" in ap.shape_guard(running)
    block = "pve-firewall status\nss -tlnp | grep 8790"
    assert "copied back without being run" in ap.shape_guard("pve-firewall status\nss -tlnp | grep 8790", block)
    assert ap.shape_guard(LIVE, block) is None                                  # a normal paste is judgeable
    assert ap.shape_guard("I did it, all good") is None                         # prose is not a paste
    assert ap.shape_guard("root@pve:~# ls /\nbin etc\nroot@pve:~#") is None      # returned prompt → not running


def test_sentinel_and_block_hash_round_trip():
    block = "pve-firewall status\nss -tlnp | grep 8790\n"
    s = ap.sentinel_for("ADD49", block)
    h = ap.block_hash(block)
    assert s == f'echo "== S:ADD49/{h} =="' and len(h) == 8
    assert ap.block_hash(block + "\n" + s) == h                                  # the sentinel does not change the hash
    assert ap.block_hash("  pve-firewall status \n\n# a comment\nss -tlnp | grep 8790") == h   # whitespace/comments don't matter
    paste = f"root@pve:~# pve-firewall status\nStatus: enabled/running\nroot@pve:~# ss -tlnp | grep 8790\nLISTEN 0 1 0.0.0.0:8790\nroot@pve:~# {s}\n== S:ADD49/{h} ==\nroot@pve:~#"
    p = ap.parse_paste(paste)
    assert p.sentinel_step == "ADD49" and p.sentinel_hash == h and p.sentinel_at_end
    m = ap.match_block(block, p)
    assert m["complete"] and m["ran"] == ["pve-firewall status", "ss -tlnp | grep 8790"] and m["skipped"] == [] and m["extra"] == []
    # the JS twin's fixture (app/ui/static/util.js blockHash) — keep in sync with tests/ui
    assert ap.block_hash("qm status 110\nqm config 110") == "e958581d"


def test_match_block_reports_skipped_edited_and_extra_commands():
    block = "cat /etc/os-release | head -1\npve-firewall status\nss -tlnp | grep 8790\nsystemctl is-active pve-firewall"
    paste = ("root@pve:~# cat /etc/os-release | head -1\nPRETTY_NAME=x\n"
             "root@pve:~# ss -tlnp | grep 8791\n"                          # edited: same head + first arg, different tail
             "root@pve:~# uptime\n 12:00 up 1 day\nroot@pve:~#")            # extra
    m = ap.match_block(block, ap.parse_paste(paste))
    assert m["ran"] == ["cat /etc/os-release | head -1"]
    assert m["skipped"] == ["pve-firewall status", "systemctl is-active pve-firewall"]
    assert m["edited"] == [("ss -tlnp | grep 8790", "ss -tlnp | grep 8791")] and m["extra"] == ["uptime"]
    assert not m["complete"]
    assert ap.issued_commands("a \\\n  b\n# c\n\nd") == ["a b", "d"]


# ---------------------------------------------------------------------------
# §17.1159 — the consumers read the pairs; the submit path refuses a partial run deterministically.
# ---------------------------------------------------------------------------

def test_paste_verdict_finds_the_issued_block_and_reports_a_partial_run():
    guide = "## 👉 Do this next\n\n**Run this now:**\n```bash\ncat /etc/os-release | head -1\npve-firewall status\n```\nThen tell me."
    partial = "root@pve:~# cat /etc/os-release | head -1\nPRETTY_NAME=x\nroot@pve:~#"
    v = ap.paste_verdict(partial, [guide])
    assert v["outcome"] == "incomplete" and v["match"]["skipped"] == ["pve-firewall status"] and "1 of 2 commands" in v["reason"]
    full = partial + "\nroot@pve:~# pve-firewall status\nStatus: enabled/running\nroot@pve:~#"
    v = ap.paste_verdict(full, [guide])
    assert v["outcome"] == "complete" and "all 2 issued" in v["summary"]
    assert ap.paste_verdict("root@pve:~# uptime\n1 day\nroot@pve:~#", [guide]) is None      # no overlap → not ours to judge
    assert ap.paste_verdict("I did it", [guide]) is None
    # the sentinel picks the block by hash even when the typed commands were edited
    block = "cat /etc/os-release | head -1\npve-firewall status"
    s = ap.sentinel_for("T6", block)
    edited = f"root@pve:~# cat /etc/os-release | head -2\nx\nroot@pve:~# pve-firewall status\nok\nroot@pve:~# {s}\n== S:T6/{ap.block_hash(block)} ==\nroot@pve:~#"
    v = ap.paste_verdict(edited, ["```bash\nuptime\n```", guide])
    assert v["block"].strip() == block and v["outcome"] == "complete"                 # sentinel at the end → the block completed


def test_wiring_verifier_turn_loop_fix_prompt_scribe_and_ledger():
    import inspect
    from app.modules import assist_agent, assist_guide, assist_turn, assist_runner_lookup
    v = inspect.getsource(assist_agent.verify_submit_outcome)
    assert "paste_verdict(evidence, _texts)" in v and '"grounded_by": "paste_parser"' in v and "paste_pairs=paste_pairs" in v
    assert v.index("paste_verdict(") < v.index("verify_step_success(")                # deterministic partial-run verdict first
    g = inspect.getsource(assist_guide.verify_step_success)
    assert "paste_pairs: str" in g and "parsed into what was typed and what each command printed" in g
    assert g.index("pairs_block + report_block") < g.index("(raw)")                      # the pairs lead, the raw tail follows
    t = inspect.getsource(assist_turn._run_turn_inner)
    assert "shape_guard(text_, _issued)" in t and 'handled["v"] = "paste_shape"' in t
    assert t.index("shape_guard(") < t.index("match_recipe(")                           # ahead of every model-driven branch
    f = inspect.getsource(assist_guide._build_fix_user_prompt) if hasattr(assist_guide, "_build_fix_user_prompt") else inspect.getsource(assist_guide)
    assert "The same paste, parsed" in f
    assert "distil facts from the OUTPUTS" in inspect.getsource(assist_guide._facts_evidence_view)
    r = inspect.getsource(assist_runner_lookup.recent_lookups)
    assert "kind IN ('submit', 'message')" in r and '"by": "operator"' in r


@pytest.mark.asyncio
async def test_ledger_includes_pasted_commands_and_names_who_ran_them():
    import datetime as dt
    from unittest.mock import AsyncMock, MagicMock
    from app.modules import assist_runner_lookup as rl
    t1 = dt.datetime(2026, 9, 21, 10, 0)
    runner_rows = [{"content": "[local-runner] ran the walkthrough's read-only look-up through your local runner (x):\n$ qm status 110\nstatus: running", "created_at": t1}]
    paste_rows = [{"content": "root@pve:~# pve-firewall status\nStatus: enabled/running\nroot@pve:~# qm status 110\nstatus: running\nroot@pve:~#", "created_at": t1}]
    calls = {"n": 0}
    async def _exec(q, params):
        calls["n"] += 1
        rows = runner_rows if calls["n"] == 1 else paste_rows
        return MagicMock(mappings=lambda: MagicMock(all=lambda: rows))
    db = MagicMock(); db.execute = _exec
    led = await rl.recent_lookups(db, "s")
    assert [(e["command"], e["by"]) for e in led] == [("qm status 110", "runner"), ("pve-firewall status", "operator")]
    hits = rl.find_repeated_lookups("```bash\npve-firewall status\n```", led)
    assert hits and "the operator ran it and pasted the result" in hits[0]["known"] and "enabled/running" in hits[0]["known"]


# ---------------------------------------------------------------------------
# §17.1167 — a multi-line paste: one prompt, N commands, output at the end.
# ---------------------------------------------------------------------------

# The live paste, verbatim (session 613dd1df, T6, turn 3008, 2026-09-20 21:53).
LIVE_BLOCK_PASTE = """root@pve:~# systemctl is-active pve-firewall
iptables -L PVEFW-HOST-IN -n -v | grep 8790
status_output=$(pve-firewall status)
echo "$status_output"
if echo "$status_output" | grep -q 'pending changes'; then
    pve-firewall compile && pve-firewall restart
    echo "Applied pending changes"
fi
active
   36  2160 RETURN     tcp  --  *      *       192.168.1.0/24       0.0.0.0/0            tcp dpt:8790
Status: enabled/running
root@pve:~#
"""

LIVE_BLOCK = """systemctl is-active pve-firewall
iptables -L PVEFW-HOST-IN -n -v | grep 8790
status_output=$(pve-firewall status)
echo "$status_output"
if echo "$status_output" | grep -q 'pending changes'; then
    pve-firewall compile && pve-firewall restart
    echo "Applied pending changes"
fi"""


def test_a_block_paste_without_its_block_is_one_command_swallowing_the_rest():
    """The shape the parser cannot resolve alone — kept as a test so the
    fallback stays honest rather than guessing."""
    p = ap.parse_paste(LIVE_BLOCK_PASTE)
    assert len(p.entries) == 1
    assert p.entries[0].command == "systemctl is-active pve-firewall"


def test_the_issued_block_splits_commands_from_output_and_the_run_is_complete():
    """Live: the operator ran all EIGHT commands; match_block reported ran=1,
    skipped=7, complete=False, and §17.1159's deterministic path returned
    `step_incomplete` — naming seven commands they had run — before the model
    saw anything. The engine manufactured its own re-ask."""
    p = ap.parse_paste(LIVE_BLOCK_PASTE, issued=LIVE_BLOCK)
    assert [e.command for e in p.entries] == ap.issued_commands(LIVE_BLOCK)
    assert len(p.groups) == 1 and len(p.groups[0]) == 8
    m = ap.match_block(LIVE_BLOCK, p)
    assert m["ran"] == ap.issued_commands(LIVE_BLOCK) and m["skipped"] == [] and m["complete"]
    v = ap.paste_verdict(LIVE_BLOCK_PASTE, ["**Run this now:**\n```bash\n" + LIVE_BLOCK + "\n```"])
    assert v["outcome"] == "complete" and "all 8 issued command(s) ran" in v["summary"]


def test_the_block_output_is_rendered_as_the_group_s_not_as_the_last_command_s():
    p = ap.parse_paste(LIVE_BLOCK_PASTE, issued=LIVE_BLOCK)
    out = ap.render_pairs(p)
    assert "[1-8]" in out and "pasted as ONE block and ALL ran" in out
    assert "not recoverable" in out              # no false attribution
    assert "1. systemctl is-active pve-firewall" in out and "8. fi" in out
    assert "Status: enabled/running" in out
    # and no member is blamed for the block's error output
    assert p.failed == []


def test_promotion_stops_at_the_first_line_that_is_not_an_issued_command():
    """A paste whose output happens to start with something unrelated is left
    exactly as it was — promotion is never a guess."""
    text = "root@pve:~# qm config 110 | grep ide2\nboot: order=scsi0;ide2\nide2: none,media=cdrom\nroot@pve:~#\n"
    p = ap.parse_paste(text, issued="qm config 110 | grep ide2\nqm status 110")
    assert len(p.entries) == 1 and not p.entries[0].grouped
    assert p.entries[0].output.startswith("boot: order=scsi0;ide2")
    # NOTE the blank-line guard in _promote_pasted_block cannot fire on a
    # LEADING blank: parse_paste drops it (an empty first output line leaves
    # `output` falsy, so the next line simply becomes the output). A blank
    # inside the output is moot — promotion has already stopped at the line
    # before it. The guard stays as a cheap belt-and-braces; this is what the
    # parser actually does, asserted so the next reader is not misled.
    p2 = ap.parse_paste("root@pve:~# a\n\nb\n", issued="a\nb")
    assert [e.command for e in p2.entries] == ["a", "b"]


def test_the_copy_button_sentinel_is_a_command_and_its_split_is_exact():
    """Live turn 3117: `echo \"== S:ADD65/f4d7d847 ==\"` was read as the OUTPUT
    of `qm guest exec …`, and the ledger then filed the echo as a command that
    had 'printed' the next command's text. The sentinel prints exactly its own
    line, so the split needs no issued block and is not a group."""
    text = ('root@pve:~# qm guest exec 106 -- systemctl is-active qemu-guest-agent\n'
            'echo "== S:ADD65/3d084e8b =="\n'
            'QEMU guest agent is not running\n'
            '== S:ADD65/3d084e8b ==\n'
            'root@pve:~#\n')
    p = ap.parse_paste(text)                      # no issued block needed
    assert [e.command for e in p.entries] == [
        "qm guest exec 106 -- systemctl is-active qemu-guest-agent",
        'echo "== S:ADD65/3d084e8b =="']
    assert p.entries[0].output == "QEMU guest agent is not running"
    assert p.entries[1].output == "== S:ADD65/3d084e8b =="
    assert p.groups == []                         # attribution is exact → no group
    assert p.sentinel_step == "ADD65" and p.sentinel_at_end


def test_parse_with_context_finds_the_block_itself_and_falls_back_cleanly():
    guidance = ["do this\n\n```bash\n" + LIVE_BLOCK + "\n```\n"]
    p = ap.parse_with_context(LIVE_BLOCK_PASTE, guidance)
    assert len(p.entries) == 8
    assert len(ap.parse_with_context(LIVE_BLOCK_PASTE, []).entries) == 1    # no block → plain parse
    assert ap.parse_with_context("", guidance).entries == []
