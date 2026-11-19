"""§17.1952 — a heredoc's body in a drafted block is a file's content, kept as written (its shebang, comments and blank
lines). Live (job 0fb4ea7f LM2, 2026-11-19): `tee /root/lm_bench.sh <<'EOF'` lost its `#!/bin/bash` line."""
from __future__ import annotations

from app.modules.runbook_templates import model_fence

_SCRIPT = "#!/bin/bash\n# the benchmark\nset -u\n\npython3 - <<'PY'\n# a python comment\nprint(1)\nPY\necho done"


def test_the_body_is_kept_and_the_block_around_it_is_still_cleaned():
    text = f"```bash\n# drop this\n$ tee /root/x.sh <<'EOF'\n{_SCRIPT}\nEOF\n\nchmod 700 /root/x.sh\n```"
    out = model_fence(text)
    assert out == f"tee /root/x.sh <<'EOF'\n{_SCRIPT}\nEOF\nchmod 700 /root/x.sh"


def test_a_dash_heredoc_ends_on_a_tab_indented_terminator_and_a_here_string_is_not_one():
    out = model_fence("```bash\ncat > /tmp/a <<-END\n\t# kept\n\tEND\n# dropped\nls <<< 'x'\n# dropped too\n```")
    assert out.splitlines() == ["cat > /tmp/a <<-END", "\t# kept", "\tEND", "ls <<< 'x'"]


def test_an_unterminated_heredoc_never_swallows_the_closing_fence():
    out = model_fence("```bash\ncat > /tmp/a <<'EOF'\n# body\n```\nafter the fence")
    assert out.splitlines() == ["cat > /tmp/a <<'EOF'", "# body"]
