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
    got = parse_file_writes(_write_block(lines=("abc", "de")))
    assert got == {_PATH: {"expected": 7, "lines": 2}}   # 3+1 + 2+1


def test_chunked_appends_sum_to_one_total():
    """§17.963 splits a write into `>` then `>>`; the ledger must see ONE file."""
    text = (_write_block(lines=("abc",), redirect=">") + "\n"
            + _write_block(lines=("de",), redirect=">>") + "\n"
            + _write_block(lines=("f",), redirect=">>"))
    assert parse_file_writes(text) == {_PATH: {"expected": 4 + 3 + 2, "lines": 3}}


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
