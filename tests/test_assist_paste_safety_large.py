"""§17.961-964 — large pastes, and commands that never give the prompt back.

Every case is from live session 613dd1df (2026-09-06), not invented.
"""
import re

import pytest

from app.modules.assist_directives import apply_truncated_paste
from app.modules.assist_guide import (
    _PASTE_SAFE_BYTES,
    find_nonterminating_commands,
    find_novel_urls,
    find_repeated_failed,
    repair_nonterminating_commands,
    split_large_paste_blocks,
)
from app.modules.assist_policy import detect_truncated_paste


# ── §17.964 — the command that left them in a program ─────────────────────
#
# T33 17:52. The engine asked them to verify the API with `pm2 logs …
# --lines 20` and "tell me what it shows". Operator: "it then is just blank, as
# if i'm in a program and its waiting for a command."

_LIVE_PM2 = "```bash\npct exec 111 -- pm2 logs control-panel-backend --lines 20\n```"


def test_the_live_pm2_logs_command_is_repaired():
    out, notes = repair_nonterminating_commands(_LIVE_PM2)
    assert "pm2 logs --nostream" in out
    assert notes and "tails forever" in notes[0]
    # --lines 20 is kept: it bounds the backlog, which is what they wanted.
    assert "--lines 20" in out


def test_pm2_logs_is_flagged_before_repair():
    hits = find_nonterminating_commands(_LIVE_PM2)
    assert hits and hits[0]["fixable"]


@pytest.mark.parametrize("cmd,expected", [
    ("tail -f /var/log/syslog", "tail -n 50 /var/log/syslog"),
    ("journalctl -u nginx -f", "journalctl -n 50 --no-pager -u nginx"),
    ("journalctl -u nginx", "journalctl --no-pager -u nginx"),
    ("systemctl status nginx", "systemctl status --no-pager nginx"),
    ("docker logs -f web", "docker logs --tail 50 web"),
    ("kubectl logs -f pod", "kubectl logs --tail=50 pod"),
    ("git log --oneline", "git --no-pager log --oneline"),
    ("watch -n 2 df -h", "df -h"),
    ("ping 192.168.1.1", "ping -c 4 192.168.1.1"),
])
def test_the_whole_class_returns_to_a_prompt(cmd, expected):
    out, _ = repair_nonterminating_commands(f"```bash\n{cmd}\n```")
    assert expected in out, out


@pytest.mark.parametrize("cmd", [
    "journalctl -n 50 --no-pager -u nginx",
    "ping -c 4 192.168.1.1",
    "pm2 logs --nostream app",
    "systemctl status --no-pager nginx",
])
def test_already_safe_commands_are_untouched(cmd):
    block = f"```bash\n{cmd}\n```"
    assert repair_nonterminating_commands(block)[0] == block


@pytest.mark.parametrize("cmd", [
    "curl -s http://192.168.1.25:3001/status",
    "pct exec 111 -- cat /opt/app/server.js",
    "ls -la /opt",
    "npm install",
])
def test_terminating_commands_are_left_alone(cmd):
    block = f"```bash\n{cmd}\n```"
    assert repair_nonterminating_commands(block)[0] == block
    assert find_nonterminating_commands(block) == []


def test_blocking_programs_are_named_with_their_exit_key():
    block = "```bash\nnpm run dev\n```"
    _, notes = repair_nonterminating_commands(block)
    assert notes and "Ctrl-C" in notes[0]


def test_a_heredoc_body_is_never_rewritten():
    """§17.956 again: `tail -f` inside a shell SCRIPT being written to disk is
    file content, not a command the operator is about to run."""
    block = ("```bash\ncat > /opt/watch.sh <<'EOF'\ntail -f /var/log/syslog\n"
             "EOF\n```")
    assert repair_nonterminating_commands(block)[0] == block


# ── §17.963 — a paste the terminal can actually swallow ───────────────────


def _big_write(n_lines=60):
    body = "\n".join(
        f"const item{i:03d} = {{ id: '{i}', name: 'Service{i}' }};"
        for i in range(n_lines))
    return ("```bash\nbash -c \"cat > /opt/app/App.jsx\" <<'EOF'\n"
            + body + "\nEOF\n```")


def test_an_oversized_write_is_split():
    src = _big_write()
    assert len(src) > _PASTE_SAFE_BYTES
    out, notes = split_large_paste_blocks(src)
    blocks = re.findall(r"```[a-z]*\n(.*?)```", out, re.S)
    assert len(blocks) > 2
    assert notes and "split a" in notes[0]


def test_every_emitted_block_fits_the_budget():
    out, _ = split_large_paste_blocks(_big_write(120))
    for b in re.findall(r"```[a-z]*\n.*?```", out, re.S):
        assert len(b) <= _PASTE_SAFE_BYTES, len(b)


def test_chunks_append_rather_than_overwrite():
    """The bug this would hide: every chunk using `>` leaves only the last."""
    out, _ = split_large_paste_blocks(_big_write())
    assert out.count('cat > /opt/app/App.jsx') == 1
    assert out.count('cat >> /opt/app/App.jsx') >= 1


def test_a_byte_count_check_is_appended():
    """The operator asked for a way to confirm a large paste was correct."""
    out, _ = split_large_paste_blocks(_big_write())
    assert "wc -c /opt/app/App.jsx" in out
    m = re.search(r"must print `(\d+)`", out)
    assert m
    body = re.search(r"<<'EOF'\n(.*?)\nEOF", _big_write(), re.S).group(1)
    assert int(m.group(1)) == sum(len(l.encode()) + 1 for l in body.splitlines())


def test_the_hung_prompt_escape_hatch_is_offered_once():
    out, _ = split_large_paste_blocks(_big_write())
    assert out.count("the paste was clipped — press Ctrl-C") == 1


def test_a_small_block_is_untouched():
    small = "```bash\npct exec 111 -- ls /opt\n```"
    assert split_large_paste_blocks(small) == (small, [])


# ── §17.962 — recognising the clip instead of debugging its shadow ────────


_GARBLED = ("```\nbash -c \"cat > /opt/app/App.jsx\" <<'EOF'\n"
            "if (loading) return <div>Loading Lab StaEOFort default App;>ton "
            "onClick={() => handlePower(s.id)}\n```")


def test_the_spliced_terminator_is_detected():
    got = detect_truncated_paste(_GARBLED)
    assert got and "EOF" in got["evidence"]


def test_a_ctrl_c_on_a_heredoc_paste_is_detected():
    got = detect_truncated_paste("^Croot@pve:~# bash -c \"cat > f <<'EOF'\nx\n")
    assert got and got["aborted"] and got["hung"]


def test_a_run_of_continuation_prompts_is_detected():
    got = detect_truncated_paste("bash -c \"cat > f <<'EOF'\n> line one\n> line two\n")
    assert got and got["hung"]


def test_clean_output_is_not_flagged():
    for clean in ("```bash\ncat > f <<'EOF'\nhello\nEOF\n```",
                  "> npx\n> create-vite /opt/ui --template react",
                  "added 143 packages",
                  ""):
        assert detect_truncated_paste(clean) is None, clean


def test_the_directive_stops_the_app_being_debugged():
    out = apply_truncated_paste("SYS", truncated=detect_truncated_paste(_GARBLED))
    assert "TRUNCATED BY THEIR TERMINAL" in out
    assert "INCOMPLETE or EMPTY" in out
    assert "chasing a ghost" in out
    assert apply_truncated_paste("SYS", truncated=None) == "SYS"


# ── §17.961 — a URL inside a file is not a prescribed URL ─────────────────


_FILE_WITH_URL = ("```bash\npct exec 111 -- bash -c \"cat > /opt/app/server.js\" "
                  "<<'EOF'\nconst u = `https://${PVE_HOST}:8006/api2/json`;\nEOF\n```")


def test_a_template_literal_url_is_not_a_repeat():
    assert find_repeated_failed(_FILE_WITH_URL,
                                "https://${PVE_HOST}:8006/api2/json") == []


def test_a_template_literal_url_has_no_provenance_to_trace():
    from app.modules.assist_guide import _text_without_heredoc_bodies
    assert find_novel_urls(_text_without_heredoc_bodies(_FILE_WITH_URL), "") == []


def test_a_real_command_url_is_still_gated():
    real = "```bash\ncurl -fsSL https://deb.nodesource.com/setup_20.x | bash -\n```"
    assert find_repeated_failed(real, "https://deb.nodesource.com/setup_20.x")
    from app.modules.assist_guide import _text_without_heredoc_bodies
    assert find_novel_urls(_text_without_heredoc_bodies(real), "no sources")


def test_the_version_guess_skeleton_match_survives():
    """§17.883 must keep working — scoping the scan must not disarm it."""
    draft = ("```bash\ncurl -o /tmp/x.tar.gz "
             "https://github.com/R/R/releases/download/v5.3.3/x.tar.gz\n```")
    assert find_repeated_failed(
        draft, "https://github.com/R/R/releases/download/v5.3.0/x.tar.gz")


# ── §17.964 precision — measured against every command this session emitted ──
#
# Scanning all 705 KB of the session's assistant output surfaced four false
# positives in the first cut of these rules. Each is pinned here.


@pytest.mark.parametrize("line", [
    # "On top of that, the DNS server ..." — `top` is an English word.
    "The container can reach the internet, but its DNS server is not resolving."
    " On top of that, the repo URL is wrong.",
    # "If you want more details" — so is `more`.
    "You should see the single word `active` printed. If you want more details,"
    " check the unit file.",
])
def test_prose_is_never_rewritten(line):
    block = f"```bash\n{line}\n```"
    assert repair_nonterminating_commands(block)[0] == block
    assert find_nonterminating_commands(block) == []


@pytest.mark.parametrize("cmd", [
    "pct exec 111 -- ln -sf /usr/local/bin/node /bin/node",   # verb is `ln`
    "pct exec 111 -- node -v",                                 # prints and exits
    "pct exec 111 -- /usr/local/bin/node -v",
    "python3 -c \"import torch; print(torch.cuda.is_available())\"",
    "node server.js",
])
def test_a_program_that_exits_is_not_called_a_repl(cmd):
    block = f"```bash\n{cmd}\n```"
    assert repair_nonterminating_commands(block)[0] == block
    assert find_nonterminating_commands(block) == []


def test_a_bare_interpreter_is_still_flagged():
    hits = find_nonterminating_commands("```bash\npct exec 111 -- python3\n```")
    assert hits and not hits[0]["fixable"]


@pytest.mark.parametrize("cmd", [
    "pct exec 102 -- ping -c1 192.168.1.1",     # `-c1`, no space
    "ping -c 1 192.168.1.1",
    "ping -c4 -W1 10.0.0.1",
])
def test_an_already_bounded_ping_is_not_double_flagged(cmd):
    """The first cut rewrote `ping -c1 X` into `ping -c 4 -c1 X`."""
    block = f"```bash\n{cmd}\n```"
    assert repair_nonterminating_commands(block)[0] == block


def test_the_live_session_has_no_false_positives_left():
    """Shapes taken verbatim from the session that produced these rules."""
    real = "\n".join([
        "```bash",
        "pct exec 111 -- pm2 logs control-panel-backend --lines 20",
        "sudo systemctl status palworld",
        "sudo journalctl -u palworld -n 50",
        "nano /etc/default/grub",
        "```",
    ])
    hits = {h["line"] for h in find_nonterminating_commands(real)}
    assert len(hits) == 4, hits
    out, notes = repair_nonterminating_commands(real)
    assert "pm2 logs --nostream" in out
    assert "systemctl status --no-pager" in out
    assert "journalctl --no-pager -u palworld -n 50" in out
    assert any("editor" in n for n in notes)          # nano: warned, not rewritten
    assert "nano /etc/default/grub" in out


# ── §17.966 — one copy of a file per reply ────────────────────────────────
#
# §17.741 mandates a leading 👉 block with the immediate action and then the
# full walkthrough. For a one-line command that repetition is the point; for a
# file it means the whole file twice. Live turn 1746 carried App.jsx complete in
# both — 73 lines each, identical once indentation is ignored, 2291 vs 2715
# bytes. The operator has to pick one, §17.963 chunks both, and §17.965 has to
# guess which byte count the file is held to.

from app.modules.assist_guide import dedupe_repeated_file_writes  # noqa: E402

_BODY = [f"const item{i:02d} = {{ id: '{i}', name: 'Service number {i}' }};"
         for i in range(20)]


def _write(path, lines, redirect=">", indent=""):
    body = "\n".join(indent + l for l in lines)
    return (f'```bash\npct exec 111 -- bash -c "cat {redirect} {path}" '
            f"<<'EOF'\n{body}\nEOF\n```")


def test_the_indented_reprint_is_collapsed():
    """The live shape exactly: flattened in the callout, indented below it."""
    reply = ("## Do this next\n" + _write("/opt/a/App.jsx", _BODY)
             + "\n\n## Fix\n" + _write("/opt/a/App.jsx", _BODY, indent="  "))
    out, notes = dedupe_repeated_file_writes(reply)
    assert out.count("<<'EOF'") == 1
    assert "run it once, not twice" in out
    assert notes and "printed twice" in notes[0]


def test_the_first_copy_is_the_one_kept():
    """§17.741 puts the actionable block first; that is what they paste."""
    reply = (_write("/opt/a/App.jsx", _BODY)
             + "\n" + _write("/opt/a/App.jsx", _BODY, indent="    "))
    out, _ = dedupe_repeated_file_writes(out_first := reply)
    kept = re.search(r"```bash\n(.*?)```", out, re.S).group(1)
    assert "    const item00" not in kept          # not the indented one
    assert out_first.index("const item00") > 0


def test_a_short_repeated_command_is_left_alone():
    """Repeating a one-liner is the §17.741 design working."""
    reply = ("## Do this next\n```bash\npct exec 111 -- pm2 restart api\n```\n"
             "## Fix\n```bash\npct exec 111 -- pm2 restart api\n```")
    assert dedupe_repeated_file_writes(reply) == (reply, [])


def test_chunked_appends_are_never_deduped():
    """§17.963 emits `cat >` then N x `cat >>` for ONE file. Collapsing those
    would silently drop most of the file."""
    reply = (_write("/opt/a/App.jsx", _BODY[:10])
             + "\n" + _write("/opt/a/App.jsx", _BODY[10:], redirect=">>"))
    assert dedupe_repeated_file_writes(reply) == (reply, [])


def test_different_paths_are_untouched():
    reply = _write("/opt/a/App.jsx", _BODY) + "\n" + _write("/opt/a/main.jsx", _BODY)
    assert dedupe_repeated_file_writes(reply) == (reply, [])


def test_a_genuinely_different_second_write_survives():
    """Same path, different content, is an edit — not a re-print."""
    reply = (_write("/opt/a/App.jsx", _BODY)
             + "\n" + _write("/opt/a/App.jsx", _BODY[:5] + ["// changed"]))
    out, notes = dedupe_repeated_file_writes(reply)
    assert out == reply and notes == []


def test_dedupe_is_idempotent():
    reply = _write("/opt/a/App.jsx", _BODY) + "\n" + _write("/opt/a/App.jsx", _BODY)
    once, _ = dedupe_repeated_file_writes(reply)
    twice, notes = dedupe_repeated_file_writes(once)
    assert twice == once and notes == []


def test_dedupe_runs_before_chunking():
    """Order matters both ways: dedupe after chunking would compare `>>`
    fragments, and chunking a duplicate doubles the operator's pastes."""
    import inspect

    from app.modules import assist_guide

    for fn in (assist_guide.generate_fix, assist_guide.generate_guidance):
        src = inspect.getsource(fn)
        assert src.index("dedupe_repeated_file_writes") < src.index(
            "split_large_paste_blocks"), fn.__name__


def test_one_file_yields_one_chunked_sequence():
    """End to end: the duplicate must not double the paste count. Live turn
    1746 chunked into SIX pastes for one 72-line file."""
    big = _BODY * 3                       # comfortably over the chunk threshold
    reply = (_write("/opt/a/App.jsx", big)
             + "\n" + _write("/opt/a/App.jsx", big, indent="  "))
    assert len(_write("/opt/a/App.jsx", big)) > 1200
    deduped, _ = dedupe_repeated_file_writes(reply)
    chunked, _ = split_large_paste_blocks(deduped)
    assert chunked.count("**Paste 1 of") == 1
