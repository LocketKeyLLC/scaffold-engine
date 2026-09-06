"""§17.965 — the engine wrote the file, so the engine knows how big it should be.

Operator: *"the engine should be able to figure it out on its own. Based on the
facts presented and recorded. As well as the research compiled it should be
simple."*

They are right, and §17.962/963 did not go far enough. Those detect a clipped
paste in the moment and split future writes into safe pieces — but both still
depend on the operator noticing something is wrong and saying so. Live (T34,
2026-09-06), nobody said so: `App.jsx` was written at roughly 1000 of its 2287
bytes, the page came up blank, and the engine debugged React for two turns
against a source file missing its last thirty lines, including
`export default App;`.

Everything needed to catch that was already on hand. The engine COMPOSED the
file, so it knows the byte count exactly. The operator pastes command output
every turn. The only missing piece was a place to write the number down and a
deterministic comparison — the same shape as the §17.914 `system_state` ledger
next door: parse the engine's own output, parse the operator's output, compare
in code, render the result into every prompt.

Nothing here calls a model. A file is the right size or it is not.
"""
from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger("scaffold")

# `cat > /path <<'EOF'` and `cat >> /path <<EOF`, wrapped in anything
# (`pct exec 111 -- bash -c "…"`, `sudo`, `docker exec`). The redirect and the
# heredoc are the two halves that matter; the wrapper is not our business.
_WRITE_OPEN_RE = re.compile(
    r"""\b(?:cat|tee)\s*(?P<append>>>?)\s*(?P<path>(?:/|\./|~/)[^\s"'`;|&)]+)"""
    r"""[^\n]*?<<-?\s*(?P<q>['"]?)(?P<delim>[A-Za-z_][A-Za-z0-9_]*)(?P=q)""",
    re.VERBOSE,
)
# `wc -c` output: "2287 /opt/app/App.jsx". Also matches `wc -c <` forms.
_WC_LINE_RE = re.compile(r"^\s*(?P<bytes>\d{1,12})\s+(?P<path>(?:/|\./|~/)\S+)\s*$")
# `ls -l` output: "-rw-r--r-- 1 root root 2287 Sep  6 19:20 /opt/app/App.jsx"
_LS_LINE_RE = re.compile(
    r"^\s*[-bcdlps][rwxsStT-]{9}[.+]?\s+\d+\s+\S+\s+\S+\s+(?P<bytes>\d{1,12})\s+"
    r"\S+\s+\S+\s+\S+\s+(?P<path>\S+)\s*$")
# `stat -c %s /path` echoed with its answer on the next line.
_STAT_ECHO_RE = re.compile(
    r"stat\s+(?:-c\s*'?%s'?\s+)(?P<path>(?:/|\./|~/)\S+)[^\n]*\n\s*(?P<bytes>\d{1,12})\s*$",
    re.MULTILINE)

_MAX_TRACKED = 25


def parse_file_writes(assistant_text: str) -> dict[str, dict[str, Any]]:
    """Files this reply tells the operator to write, and their exact size.

    A `>` write starts the count over; `>>` adds to it. That is exactly how the
    §17.963 chunked form works, so a three-paste write records one total rather
    than three separate truths.
    """
    out: dict[str, dict[str, Any]] = {}
    text = assistant_text or ""
    for m in _WRITE_OPEN_RE.finditer(text):
        path = m.group("path")
        delim = m.group("delim")
        rest = text[m.end():]
        body_lines: list[str] = []
        for line in rest.splitlines()[1:] if rest.startswith("\n") else rest.splitlines():
            if line.strip().rstrip("\"'`);") == delim:
                break
            body_lines.append(line)
        else:
            continue                       # unterminated — do not record a guess
        nbytes = sum(len(ln.encode("utf-8")) + 1 for ln in body_lines)
        if not nbytes:
            continue
        rec = out.get(path)
        if rec and m.group("append") == ">>":
            rec["expected"] += nbytes
            rec["lines"] += len(body_lines)
        elif rec:
            # A SECOND `>` to the same path in one reply is the engine printing
            # the file twice, not writing it twice. Live (turn 1746): the same
            # 72 lines appeared flattened in the 👉 block and indented again
            # below it — 2287 bytes and 2711 bytes for one file. The operator
            # pastes the leading block (§17.741 mandates it leads), so that is
            # the one to hold ourselves to.
            logger.info("assist_file_write_duplicated path=%s first=%d second=%d",
                        path, rec["expected"], nbytes)
        else:
            out[path] = {"expected": nbytes, "lines": len(body_lines)}
    return out


def parse_file_sizes(operator_text: str) -> dict[str, int]:
    """Sizes the operator's own pasted output reports, by path."""
    out: dict[str, int] = {}
    for line in (operator_text or "").splitlines():
        for rx in (_WC_LINE_RE, _LS_LINE_RE):
            m = rx.match(line)
            if m and m.group("path").startswith(("/", "./", "~/")):
                out[m.group("path")] = int(m.group("bytes"))
                break
    for m in _STAT_ECHO_RE.finditer(operator_text or ""):
        out[m.group("path")] = int(m.group("bytes"))
    return out


def merge_file_writes(current: dict | None,
                      written: dict | None = None,
                      observed: dict | None = None) -> dict:
    """Fold a new write and/or a new observation into the ledger."""
    merged: dict[str, dict[str, Any]] = {
        k: dict(v) for k, v in (current or {}).items() if isinstance(v, dict)
    }
    for path, rec in (written or {}).items():
        # A fresh write supersedes the previous expectation AND its observation:
        # the old size is no longer the thing being checked.
        merged[path] = {"expected": rec.get("expected"),
                        "lines": rec.get("lines"),
                        "observed": None}
    for path, size in (observed or {}).items():
        merged.setdefault(path, {"expected": None, "lines": None})
        merged[path]["observed"] = int(size)
    return dict(list(merged.items())[-_MAX_TRACKED:])


def find_size_mismatches(state: dict | None) -> list[dict]:
    """Files whose measured size is not the size the engine wrote.

    A SHORT file is the §17.962 signature — a clipped paste. A file that is
    merely different is still wrong, and either way the contents cannot be
    trusted by anything downstream.
    """
    hits: list[dict] = []
    for path, rec in (state or {}).items():
        if not isinstance(rec, dict):
            continue
        exp, obs = rec.get("expected"), rec.get("observed")
        if isinstance(exp, int) and isinstance(obs, int) and exp != obs:
            hits.append({
                "path": path, "expected": exp, "observed": obs,
                "short": obs < exp,
                "missing": exp - obs,
            })
    return hits


def render_file_writes(state: dict | None) -> str:
    """The prompt block. A record the model cannot see is not a record (§17.913)."""
    rows = [(p, r) for p, r in (state or {}).items()
            if isinstance(r, dict) and isinstance(r.get("expected"), int)]
    if not rows:
        return ""
    lines = ["### FILES THIS SESSION WROTE (byte counts are exact — the engine "
             "composed these files, so a different size on disk means the write "
             "did not land whole):"]
    for path, rec in rows[-10:]:
        exp, obs = rec["expected"], rec.get("observed")
        if not isinstance(obs, int):
            lines.append(f"- `{path}` — should be {exp} bytes (not yet checked)")
        elif obs == exp:
            lines.append(f"- `{path}` — {exp} bytes, VERIFIED intact")
        else:
            lines.append(
                f"- `{path}` — should be {exp} bytes, measured **{obs}**. "
                f"{'TRUNCATED, missing ' + str(exp - obs) + ' bytes' if obs < exp else 'WRONG SIZE'}. "
                "Its contents are not what you wrote; rewrite it in pieces "
                "before drawing any conclusion from how the program behaves.")
    return "\n".join(lines)
