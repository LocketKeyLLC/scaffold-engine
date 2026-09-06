"""§17.965 — the engine wrote the file, so it knows how big it should be.

Operator: *"the engine should be able to figure it out on its own. Based on the
facts presented and recorded."*

Live (T34, 2026-09-06): App.jsx landed at ~1000 of its 2287 bytes, `export
default App;` never arrived, the page came up blank, and the engine debugged
React for two turns against a half-written source file. Every input needed to
catch that was already in hand.
"""
import inspect

import pytest

from app.modules.assist_directives import apply_file_mismatch
from app.modules.assist_files import (
    find_size_mismatches,
    merge_file_writes,
    parse_file_sizes,
    parse_file_writes,
    render_file_writes,
)

_PATH = "/opt/control-panel-ui/src/App.jsx"


def _write_block(path=_PATH, lines=("a = 1;", "b = 2;"), redirect=">"):
    body = "\n".join(lines)
    return (f'```bash\npct exec 111 -- bash -c "cat {redirect} {path}" '
            f"<<'EOF'\n{body}\nEOF\n```")


# ── recording what the engine wrote ───────────────────────────────────────


def test_a_write_records_its_exact_byte_count():
    got = parse_file_writes(_write_block(lines=("abc", "de")))[_PATH]
    assert got["expected"] == 7 and got["lines"] == 2    # 3+1 + 2+1
    assert got["sha"]                                    # §17.967 fingerprint


def test_chunked_appends_sum_to_one_total():
    """§17.963 splits a write into `>` then `>>`; the ledger must see ONE file."""
    text = (_write_block(lines=("abc",), redirect=">") + "\n"
            + _write_block(lines=("de",), redirect=">>") + "\n"
            + _write_block(lines=("f",), redirect=">>"))
    got = parse_file_writes(text)[_PATH]
    assert got["expected"] == 4 + 3 + 2 and got["lines"] == 3


def test_the_same_file_printed_twice_is_not_written_twice():
    """Live turn 1746 carried the same 72 lines twice — flattened in the 👉
    block (2287 bytes) and indented below it (2711). The operator pastes the
    leading one, so that is the number to hold ourselves to."""
    text = _write_block(lines=("abc",)) + "\n" + _write_block(lines=("  abc",))
    assert parse_file_writes(text)[_PATH]["expected"] == 4


def test_an_unterminated_heredoc_records_nothing():
    """No terminator means no reliable count; a guessed expectation would
    manufacture a mismatch out of nothing."""
    text = f'```bash\ncat > {_PATH} <<\'EOF\'\nabc\n```'
    assert parse_file_writes(text) == {}


def test_prose_and_ordinary_commands_record_nothing():
    for t in ("run `ls -l /opt` and tell me what it shows",
              "```bash\npct exec 111 -- cat /opt/app.js\n```", ""):
        assert parse_file_writes(t) == {}


# ── reading the size back out of the operator's paste ─────────────────────


def test_a_wc_c_paste_is_read():
    paste = (f'root@pve:~# pct exec 111 -- bash -c "wc -c {_PATH}"\n'
             f"1004 {_PATH}")
    assert parse_file_sizes(paste) == {_PATH: 1004}


def test_an_ls_l_paste_is_read():
    paste = f"-rw-r--r-- 1 root root 2287 Sep  6 19:20 {_PATH}"
    assert parse_file_sizes(paste) == {_PATH: 2287}


def test_a_stat_paste_is_read():
    assert parse_file_sizes(f"# stat -c %s {_PATH}\n2287") == {_PATH: 2287}


def test_unrelated_numbers_are_not_sizes():
    for noise in ("added 143 packages, and audited 144 packages in 16s",
                  "1|control- | Backend API running on port 3001",
                  "v20.20.2"):
        assert parse_file_sizes(noise) == {}


# ── the comparison, which is the whole point ──────────────────────────────


def test_the_live_truncation_is_caught_without_being_told():
    state = merge_file_writes(
        {}, written={_PATH: {"expected": 2287, "lines": 72}},
        observed={_PATH: 1004})
    hits = find_size_mismatches(state)
    assert hits == [{"path": _PATH, "expected": 2287, "observed": 1004,
                     "short": True, "missing": 1283}]


def test_a_matching_size_is_silent():
    state = merge_file_writes({}, written={_PATH: {"expected": 2287, "lines": 72}},
                              observed={_PATH: 2287})
    assert find_size_mismatches(state) == []
    assert "VERIFIED intact" in render_file_writes(state)


def test_an_unchecked_file_is_not_a_mismatch():
    state = merge_file_writes({}, written={_PATH: {"expected": 2287, "lines": 72}})
    assert find_size_mismatches(state) == []
    assert "not yet checked" in render_file_writes(state)


def test_a_rewrite_clears_the_old_observation():
    """Otherwise the stale 1004 would keep accusing a file that was just
    rewritten correctly."""
    state = merge_file_writes({}, written={_PATH: {"expected": 2287, "lines": 72}},
                              observed={_PATH: 1004})
    assert find_size_mismatches(state)
    state = merge_file_writes(state, written={_PATH: {"expected": 2287, "lines": 72}})
    assert find_size_mismatches(state) == []


def test_the_rendered_block_states_the_subtraction():
    state = merge_file_writes({}, written={_PATH: {"expected": 2287, "lines": 72}},
                              observed={_PATH: 1004})
    block = render_file_writes(state)
    assert "2287" in block and "1004" in block
    assert "TRUNCATED, missing 1283 bytes" in block
    assert "before drawing any conclusion" in block


def test_no_writes_renders_nothing():
    assert render_file_writes({}) == ""
    assert render_file_writes(None) == ""


# ── the directive: stop debugging the symptom ─────────────────────────────


def test_the_directive_forbids_debugging_the_app():
    out = apply_file_mismatch("SYS", mismatches=[
        {"path": _PATH, "expected": 2287, "observed": 1004, "short": True,
         "missing": 1283}])
    assert "WRONG SIZE ON DISK" in out
    assert "Do NOT debug the application" in out
    assert "1283 bytes missing" in out
    assert apply_file_mismatch("SYS", mismatches=[]) == "SYS"
    assert apply_file_mismatch("SYS", mismatches=None) == "SYS"


# ── wiring: recorded, observed, rendered, acted on ────────────────────────


def test_the_ledger_is_recorded_observed_rendered_and_acted_on():
    from app.modules import assist_guide, assist_memory, assist_render, assist_turns

    assert "parse_file_writes" in inspect.getsource(
        assist_turns.capture_assistant_reply)            # the engine's own output
    assert "parse_file_sizes" in inspect.getsource(assist_memory)   # the operator's
    assert "render_file_writes" in inspect.getsource(assist_render)  # every prompt
    fix = inspect.getsource(assist_guide.generate_fix)
    assert "find_size_mismatches" in fix and "apply_file_mismatch" in fix


# ── §17.967 — compare the CONTENT, not just the length ────────────────────
#
# Operator: "It needs to review the users pasted response and compare what is
# recorded to work."
#
# Live T34 (22:41-23:01): the operator pasted the whole of App.jsx back. It was
# complete, it ended in `export default App;`, and it matched what the engine
# had composed. The engine rewrote it anyway, three turns running, while the
# real cause sat in two files it had itself written — a backend returning an
# object where the frontend calls `.find` on it.

from app.modules.assist_files import (  # noqa: E402
    _as_written_to_disk,
    _heredoc_is_quote_nested,
    content_fingerprint,
    find_content_mismatches,
    find_verified_file_rewrites,
    parse_file_contents,
    verified_files,
)

_NESTED = 'pct exec 111 -- bash -c "cat > /opt/a/App.jsx <<\'EOF\''
_OUTER = 'pct exec 111 -- bash -c "cat > /opt/a/App.jsx" <<\'EOF\''


# The escaping layer — the whole difficulty, and the thing that would have made
# naive comparison worse than none.


def test_a_quote_nested_heredoc_is_recognised():
    assert _heredoc_is_quote_nested(_NESTED)
    assert not _heredoc_is_quote_nested(_OUTER)
    assert not _heredoc_is_quote_nested("cat > /opt/a/App.jsx <<'EOF'")


def test_the_shell_layer_is_resolved_before_comparing():
    """Live: the reply says fetch(\\`\\${API_BASE}\\`) and the file correctly
    holds fetch(`${API_BASE}`). Comparing those raw reports a difference on
    every write containing a template literal."""
    emitted = r"fetch(\`\${API_BASE}/status\`)"
    on_disk = "fetch(`${API_BASE}/status`)"
    assert _as_written_to_disk(_NESTED, emitted) == on_disk
    # Outside the quotes there is no shell layer to undo.
    assert _as_written_to_disk(_OUTER, on_disk) == on_disk


def test_normalisation_ignores_trailing_space_and_blank_lines_only():
    """Leading indentation IS content in a source file — unlike §17.966's
    dedupe, where it was the only difference between two printings of one
    file. Trailing whitespace and blank lines are not."""
    assert content_fingerprint("a\nb") == content_fingerprint("a  \n\nb\n")
    assert content_fingerprint("a\nb") != content_fingerprint("a\n  b")
    assert content_fingerprint("a\nb") != content_fingerprint("a\nB")


# Reading the file back out of the operator's paste.


def test_the_pasted_file_is_extracted():
    paste = ('root@pve:~# pct exec 111 -- bash -c "cat /opt/a/App.jsx"\n'
             "line one\nline two\nline three\n"
             "root@pve:~# pm2 restart control-panel\n[PM2] done")
    got = parse_file_contents(paste, ["/opt/a/App.jsx"])
    assert got == {"/opt/a/App.jsx": "line one\nline two\nline three"}


def test_only_known_paths_are_extracted():
    """An unrelated `cat` must not invent a ledger entry."""
    paste = "root@pve:~# cat /etc/hosts\n127.0.0.1 localhost\nroot@pve:~# "
    assert parse_file_contents(paste, ["/opt/a/App.jsx"]) == {}


# The comparison, and the loop it ends.


def _ledger(written_body, pasted_body, open_line=_NESTED):
    written = {"/opt/a/App.jsx": {
        "expected": 100, "lines": len(written_body.splitlines()),
        "sha": content_fingerprint(_as_written_to_disk(open_line, written_body))}}
    return merge_file_writes({}, written=written,
                             contents={"/opt/a/App.jsx": pasted_body})


def test_a_matching_paste_marks_the_file_verified():
    st = _ledger(r"const a = \`\${X}\`;", "const a = `${X}`;")
    assert verified_files(st) == ["/opt/a/App.jsx"]
    assert find_content_mismatches(st) == []
    block = render_file_writes(st)
    assert "VERIFIED CORRECT on disk" in block
    assert "Do NOT rewrite it" in block
    assert "different cause" in block


def test_a_differing_paste_is_reported_as_such():
    st = _ledger("const a = 1;\nconst b = 2;", "const a = 1;")
    assert verified_files(st) == []
    assert find_content_mismatches(st) == [
        {"path": "/opt/a/App.jsx", "expected_lines": 2, "observed_lines": 1}]
    assert "does NOT match" in render_file_writes(st)


def test_a_fresh_write_clears_the_verification():
    """A rewritten file has to be proven again; the old hash says nothing."""
    st = _ledger("a", "a")
    assert verified_files(st)
    st = merge_file_writes(st, written={"/opt/a/App.jsx": {
        "expected": 9, "lines": 1, "sha": "deadbeefdeadbeef"}})
    assert verified_files(st) == []


# The gate: rewriting a verified file is not a judgement call.


def test_rewriting_a_verified_file_is_a_violation():
    draft = ('```bash\npct exec 111 -- bash -c "cat > /opt/a/App.jsx" '
             "<<'EOF'\nx\nEOF\n```")
    assert find_verified_file_rewrites(draft, ["/opt/a/App.jsx"])


def test_reading_a_verified_file_is_fine():
    draft = "```bash\npct exec 111 -- cat /opt/a/App.jsx\n```"
    assert find_verified_file_rewrites(draft, ["/opt/a/App.jsx"]) == []


def test_writing_a_different_file_is_fine():
    draft = "```bash\ncat > /opt/a/main.jsx <<'EOF'\nx\nEOF\n```"
    assert find_verified_file_rewrites(draft, ["/opt/a/App.jsx"]) == []


def test_the_gate_and_the_ledger_reach_the_fix_path():
    from app.modules import assist_guide, assist_memory

    fix = inspect.getsource(assist_guide.generate_fix)
    assert "find_verified_file_rewrites" in fix
    assert "verified_files" in fix
    assert "pasted back by the operator and match" in fix   # regeneration notice
    assert "parse_file_contents" in inspect.getsource(assist_memory)
