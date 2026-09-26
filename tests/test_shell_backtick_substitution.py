"""§17.1180e — an UNESCAPED backtick inside a double-quoted shell string is a
command substitution, not punctuation.

Written after planting one in `scripts/doctor.sh`:

    warn "${dangling} dangling images — `make build` prunes those over 24h…"

`doctor.sh` is a read-only diagnostic. That line would have REBUILT AND
RESTARTED THE ENGINE as a side effect of reporting a warning — and only in the
warning branch, so it would have sat dormant until the day the disk filled,
which is the worst possible moment to trigger an unexpected deploy.

Escaped backticks (\\`) are literal and fine; five scripts use them correctly to
quote a command inside a message. This gate only catches the unescaped form.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = sorted(p for p in ROOT.rglob("*.sh")
               if ".git" not in p.parts and "node_modules" not in p.parts)


def _offenders(text: str) -> list[tuple[int, str]]:
    """Lines where a double-quoted string contains an unescaped backtick.

    Scans character by character rather than by regex: quoting state is what
    decides whether a backtick substitutes, and a regex cannot track it.
    """
    out = []
    for n, line in enumerate(text.split("\n"), 1):
        if line.lstrip().startswith("#"):
            continue
        in_dq = in_sq = False
        i = 0
        while i < len(line):
            c = line[i]
            if c == "\\":
                i += 2                      # escaped: consume the pair
                continue
            if c == "'" and not in_dq:
                in_sq = not in_sq
            elif c == '"' and not in_sq:
                in_dq = not in_dq
            elif c == "`" and in_dq:
                out.append((n, line.strip()[:90]))
                break
            i += 1
    return out


@pytest.mark.parametrize("path", SHELL, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_unescaped_backtick_inside_a_double_quoted_string(path):
    bad = _offenders(path.read_text(encoding="utf-8", errors="replace"))
    assert not bad, (
        f"{path.relative_to(ROOT)} has command substitution inside a quoted "
        "string — the shell RUNS it. Use single quotes around the command name, "
        "or escape the backtick:\n  "
        + "\n  ".join(f"line {n}: {t}" for n, t in bad)
    )


def test_the_scanner_catches_the_real_thing_and_allows_the_safe_forms():
    """Proof it is not vacuous: the exact line that was written, plus the four
    shapes that must NOT be flagged."""
    assert _offenders('warn "${d} images — `make build` prunes them"'), \
        "the scanner missed a live command substitution"
    assert _offenders('echo "before `date` after"')
    # safe: escaped, single-quoted, outside any string, and a comment
    assert not _offenders('ok "run \\`scripts/x.py\\` to confirm"')
    assert not _offenders("echo 'literal `backtick` here'")
    assert not _offenders("out=`date`")          # substitution, but intentional and unquoted
    assert not _offenders('# a comment about `make build`')
