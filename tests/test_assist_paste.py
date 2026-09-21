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
