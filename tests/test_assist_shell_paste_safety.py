"""§17.956-957 — what the operator PASTES has to survive their shell.

Both cases are reconstructed from live session 613dd1df/T33 (2026-09-06), where
the operator spent three consecutive turns trying to write one `server.js` into
an LXC container. Not invented input.
"""
from app.modules.assist_guide import (
    _command_corpus,
    _history_scannable_lines,
    find_history_expansion_hazards,
    find_repeated_failed,
    repair_history_expansion,
)


# The engine's actual command, trimmed. `bash -c "…"` wrapping a heredoc.
_SERVER_JS_FIX = """```bash
pct exec 111 -- bash -c "cat > /opt/control-panel-backend/server.js <<'EOF'
const express = require('express');
const SERVICES = {
'jellyfin': 101,
'prowlarr': 102,
'ai-vm': 110
};
app.post('/power', async (req, res) => {
const id = SERVICES[service];
if (!id) return res.status(400).json({ error: 'Invalid service' });
});
EOF"
```"""


# ── §17.956 — a heredoc body is a FILE, not a command list ────────────────


def test_heredoc_body_lines_are_not_indexed_as_commands():
    """Live: `'ai-vm': 110` and `'jellyfin': 101,` were reported to the operator
    as "already tried on this step and did not resolve it" — three turns
    running, stapled above a correct fix."""
    corpus = _command_corpus(_SERVER_JS_FIX, fenced=True)
    assert not any(c.startswith("'jellyfin'") for c in corpus), corpus
    assert not any(c.startswith("'ai-vm'") for c in corpus), corpus
    assert not any(c.startswith("const ") for c in corpus), corpus


def test_the_command_that_opens_the_heredoc_is_still_indexed():
    corpus = _command_corpus(_SERVER_JS_FIX, fenced=True)
    assert any(c.startswith("pct exec 111 -- bash -c") for c in corpus)


def test_the_whole_block_signature_still_carries_the_content():
    """Two different files written to the same path must stay DIFFERENT
    actions, so the block-level signature keeps the body."""
    other = _SERVER_JS_FIX.replace("'prowlarr': 102,", "'sonarr': 104,")
    assert _command_corpus(_SERVER_JS_FIX, fenced=True) != _command_corpus(
        other, fenced=True)


def test_file_content_no_longer_reads_as_a_repeat():
    """End to end: the previous attempt's file content must not make the next
    attempt look like a repeat."""
    previous_attempt = "'jellyfin': 101,\n\n'ai-vm': 110"
    assert find_repeated_failed(_SERVER_JS_FIX, previous_attempt) == []


def test_an_unterminated_heredoc_swallows_the_rest():
    """Conservative direction: a missed repeat costs a warning, file content in
    the operator's face costs their trust in the banner."""
    block = "```bash\ncat > /tmp/f <<'EOF'\nrm -rf /important\n```"
    assert not any("rm -rf" in c for c in _command_corpus(block, fenced=True)
                   if not c.startswith("cat >"))


# ── §17.957 — history expansion eats the paste ────────────────────────────
#
# Measured under a pty, and the two shapes behave OPPOSITELY:
#   cat > f <<'EOF' … EOF        → real heredoc, body NOT expanded
#   bash -c "cat > f <<'EOF' … EOF"  → `<<` is inside an open double quote, so
#                                      the outer shell reads continuation lines
#                                      and DOES expand — `!id: event not found`

_TRUE_HEREDOC = """```bash
cat > /opt/x/server.js <<'EOF'
if (!id) return 1;
EOF
```"""


def test_a_heredoc_nested_in_a_quoted_bash_c_is_flagged():
    hits = find_history_expansion_hazards(_SERVER_JS_FIX)
    assert hits and hits[0]["token"].startswith("!id")


def test_a_real_outer_heredoc_is_not_flagged():
    """Its body is genuinely safe — flagging it would add a pointless `set +H`
    to every file the engine ever writes."""
    assert find_history_expansion_hazards(_TRUE_HEREDOC) == []
    assert _history_scannable_lines("cat <<'EOF'\n!boom\nEOF") == [
        "cat <<'EOF'", "EOF"]


def test_inert_bangs_are_left_alone():
    """Only a `!` bash would read as an event designator counts."""
    benign = ('```bash\ntest "$a" != "$b" && echo ok\necho "all done!"\n'
              "echo 'literal !bang'\necho \"escaped \\!bang\"\n```")
    assert find_history_expansion_hazards(benign) == []
    assert repair_history_expansion(benign)[0] == benign


def test_repair_prefixes_set_h_and_explains_itself():
    out, notes = repair_history_expansion(_SERVER_JS_FIX)
    assert out.split("```bash\n", 1)[1].startswith("set +H\n")
    assert notes and "set +H" in notes[0]
    # The command itself is untouched — the fix is additive.
    assert "if (!id) return res.status(400)" in out


def test_repair_is_idempotent():
    once, _ = repair_history_expansion(_SERVER_JS_FIX)
    twice, notes = repair_history_expansion(once)
    assert twice == once and notes == []


def test_repair_is_wired_into_the_fix_and_guide_paths():
    import inspect

    from app.modules import assist_guide

    for fn in (assist_guide.generate_fix, assist_guide.generate_guidance):
        assert "repair_history_expansion" in inspect.getsource(fn), fn.__name__
