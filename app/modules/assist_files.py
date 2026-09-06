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

import hashlib
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
        # §17.967 — what the DISK will hold, past the shell's escaping.
        open_line = text[text.rfind("\n", 0, m.start()) + 1:m.end()]
        disk_body = _as_written_to_disk(open_line, "\n".join(body_lines))
        rec = out.get(path)
        if rec and m.group("append") == ">>":
            rec["expected"] += nbytes
            rec["lines"] += len(body_lines)
            rec["body"] = rec.get("body", "") + "\n" + disk_body
            rec["sha"] = content_fingerprint(rec["body"])
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
            out[path] = {"expected": nbytes, "lines": len(body_lines),
                         "body": disk_body,
                         "sha": content_fingerprint(disk_body)}
    for rec in out.values():
        rec.pop("body", None)          # the hash is the record; the text is not
    return out


# §17.967 — comparing the file's CONTENT, not just its length.
#
# Operator: *"It needs to review the users pasted response and compare what is
# recorded to work."*
#
# Live (T34, 22:41-23:01): the operator pasted the whole of `App.jsx` back from
# `cat`. It was complete, it ended in `export default App;`, and it was
# character-for-character what the engine had composed. The engine rewrote it
# anyway — three times (turns 1754, 1756, 1760) — because a byte count alone
# never arrived, and nothing else compared the paste to the record. Every one of
# those turns was spent re-fixing a file that was already right, while the real
# cause sat untouched in two files the engine had itself written: the backend
# returns an object keyed by name, the frontend calls `services.find(...)` on it,
# and `.find` is not a function on an object.
#
# THE ESCAPING LAYER IS THE WHOLE DIFFICULTY. What the engine emits is not what
# lands on disk. For a heredoc nested inside `bash -c "…"` the outer double
# quote consumes one level of backslashes first (§17.960 puts them there on
# purpose), so the reply says
#
#     fetch(\`\${API_BASE}/status\`)
#
# and the file correctly contains
#
#     fetch(`${API_BASE}/status`)
#
# Comparing those raw reports a difference on every single write that contains a
# template literal — which is worse than not comparing at all, because it would
# send the engine off rewriting correct files with total confidence. Resolving
# the escaping first, on the live pair, turns 2 spurious differences into 0.

_DQUOTE_ESCAPE_RE = re.compile(r'\\([$`"\\])')


def _heredoc_is_quote_nested(open_line: str) -> bool:
    """Is the heredoc inside an open double-quoted string on its own line?

    `bash -c "cat > f <<'EOF'`  -> one unescaped quote before `<<`  -> nested
    `bash -c "cat > f" <<'EOF'` -> two                              -> not
    """
    head = open_line.split("<<", 1)[0]
    quotes = 0
    j = 0
    while j < len(head):
        if head[j] == "\\":
            j += 2
            continue
        if head[j] == '"':
            quotes += 1
        j += 1
    return quotes % 2 == 1


def _as_written_to_disk(open_line: str, body: str) -> str:
    """The bytes the file will actually hold, after the shell has had its turn."""
    return _DQUOTE_ESCAPE_RE.sub(r"\1", body) if _heredoc_is_quote_nested(open_line) else body


def normalize_content(text: str) -> str:
    """Trailing whitespace and blank lines are not content differences."""
    return "\n".join(ln.rstrip() for ln in (text or "").splitlines() if ln.strip())


def content_fingerprint(text: str) -> str:
    return hashlib.sha256(normalize_content(text).encode("utf-8")).hexdigest()[:16]


def parse_file_contents(operator_text: str, known_paths) -> dict[str, str]:
    """File bodies the operator pasted back, by path.

    Reads `cat <path>` echoes — the shape the engine itself asks for. Bounded to
    paths already in the ledger, so an unrelated `cat` cannot invent an entry.
    """
    out: dict[str, str] = {}
    text = operator_text or ""
    for path in sorted(known_paths or (), key=len, reverse=True):
        for m in re.finditer(re.escape(path) + r'"?\s*\n', text):
            tail = text[m.end():]
            body: list[str] = []
            for line in tail.splitlines():
                # The next shell prompt ends the file.
                if re.match(r"^[a-zA-Z0-9_.-]+@[a-zA-Z0-9_.-]+:.*[#$]\s", line) \
                        or re.match(r"^root@\S+:.*#", line):
                    break
                body.append(line)
            if len(body) >= 2:
                out[path] = "\n".join(body)
                break
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
                      observed: dict | None = None,
                      contents: dict | None = None) -> dict:
    """Fold a new write, a new size, and/or pasted-back content into the ledger."""
    merged: dict[str, dict[str, Any]] = {
        k: dict(v) for k, v in (current or {}).items() if isinstance(v, dict)
    }
    for path, rec in (written or {}).items():
        # A fresh write supersedes the previous expectation AND everything
        # observed about the old one: the file being checked has changed.
        merged[path] = {"expected": rec.get("expected"),
                        "lines": rec.get("lines"),
                        "sha": rec.get("sha"),
                        "observed": None,
                        "observed_sha": None}
    for path, size in (observed or {}).items():
        merged.setdefault(path, {"expected": None, "lines": None, "sha": None})
        merged[path]["observed"] = int(size)
    for path, body in (contents or {}).items():
        merged.setdefault(path, {"expected": None, "lines": None, "sha": None})
        merged[path]["observed_sha"] = content_fingerprint(body)
        merged[path]["observed_lines"] = len(normalize_content(body).splitlines())
    return dict(list(merged.items())[-_MAX_TRACKED:])


def verified_files(state: dict | None) -> list[str]:
    """Paths whose pasted-back content matches what the engine wrote.

    This is the answer to "is the file the problem?" and it is a hash compare,
    not an opinion. A file on this list must not be rewritten again.
    """
    out = []
    for path, rec in (state or {}).items():
        if not isinstance(rec, dict):
            continue
        if rec.get("sha") and rec.get("sha") == rec.get("observed_sha"):
            out.append(path)
    return out


def find_content_mismatches(state: dict | None) -> list[dict]:
    """Files whose pasted-back content is NOT what the engine wrote."""
    hits = []
    for path, rec in (state or {}).items():
        if not isinstance(rec, dict):
            continue
        sha, obs = rec.get("sha"), rec.get("observed_sha")
        if sha and obs and sha != obs:
            hits.append({"path": path,
                         "expected_lines": rec.get("lines"),
                         "observed_lines": rec.get("observed_lines")})
    return hits


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
        # §17.967 — a content match is stronger evidence than any byte count and
        # is stated first, because it is the fact that ends the rewrite loop.
        if rec.get("sha") and rec.get("sha") == rec.get("observed_sha"):
            lines.append(
                f"- `{path}` — **VERIFIED CORRECT on disk.** The operator pasted "
                "this file back and it matches what you wrote, exactly. Do NOT "
                "rewrite it, do not re-send it in pieces, and do not treat it as "
                "a suspect. Whatever symptom remains has a different cause — "
                "look at the OTHER files and services this session set up, and "
                "at how they agree with each other.")
            continue
        if rec.get("sha") and rec.get("observed_sha"):
            lines.append(
                f"- `{path}` — pasted back and it does NOT match what you wrote "
                f"({rec.get('observed_lines')} lines on disk vs {rec.get('lines')} "
                "written). Rewrite it in pieces.")
            continue
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


def find_verified_file_rewrites(text_out: str, verified: list[str] | None) -> list[dict]:
    """§17.967 — a draft that rewrites a file already proven correct.

    Live, three turns in a row (1754, 1756, 1760) re-sent `App.jsx` after the
    operator had pasted it back intact. Each rewrite cost a full turn, taught
    the operator nothing, and left the real cause — a backend returning an
    object where the frontend calls `.find` — untouched in two files the engine
    had itself written.

    Rewriting a verified file is not a judgement call the model gets to make:
    the content matched by hash. This is the deterministic backstop behind the
    ledger's prose, in the §17.882 tradition — prompt rules get ignored.
    """
    hits: list[dict] = []
    if not verified or not (text_out or "").strip():
        return hits
    for path in verified:
        # Only a WRITE counts. `cat <path>` to inspect it is entirely fine.
        if re.search(r"\b(?:cat|tee)\s*>>?\s*\"?" + re.escape(path), text_out or ""):
            hits.append({"path": path})
    return hits
