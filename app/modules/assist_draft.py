"""§17.1178 — the DRAFT layer: what a walkthrough says, checked and repaired.

Extracted from ``assist_guide.py``, which had grown to 6,887 lines and 208
top-level names holding five unrelated jobs at once: the guidance system-prompt
assembly, these deterministic draft checks, the fix generator, guidance caching
and staleness, and the streaming generator. §17.856 already split that module
once (3,757 → 2,540 by its own record); it grew back, which is what happens when
the only thing holding a file together is history.

Everything here is PURE — a draft string in, findings or a repaired string out.
No database, no model, no settings. That is why it is the natural seam: these
are the functions a test can exercise directly, and the ones that were hardest
to find inside the generator that calls them.

Two kinds of function, by the convention the §-entries already used:

* ``find_*`` — report what is wrong with a draft, as a list of findings. Used
  by the guide/fix funnels to decide whether to redraw.
* ``repair_*`` / ``dedupe_*`` / ``split_*`` — return a corrected draft plus the
  list of what was changed, so the caller can tell the operator.

Every name is re-exported from ``assist_guide`` (the §17.856 pattern), so no
caller, test or gate has to know this file exists.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from typing import Optional

logger = logging.getLogger("scaffold.assist_guide")


_LOCAL_HOST_RE = None


def _normalized_commands(text_: str) -> set[str]:
    """§17.882 — fenced commands + bare URLs from a walkthrough, normalized
    (whitespace-collapsed) for repeat detection.

    §17.882b — URLs on the operator's OWN hosts (localhost/127.x/RFC1918) are
    EXCLUDED from URL-level matching: they're verification endpoints (`curl
    localhost:7878`) that legitimately recur in every fix. Live false positive:
    an otherwise-correct method-changing regen got a repeat warning because it
    re-checked the same local health URL. External URLs (the guessed dead
    download hosts — the real signal) still match; identical whole fenced
    blocks still match regardless."""
    global _LOCAL_HOST_RE
    import re as _re
    if _LOCAL_HOST_RE is None:
        _LOCAL_HOST_RE = _re.compile(
            r"^https?://(localhost|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|"
            r"192\.168\.\d+\.\d+|172\.(1[6-9]|2\d|3[01])\.\d+\.\d+|\[::1\])"
            r"(?=[:/]|$)", _re.I,
        )
    out: set[str] = set()
    for block in _re.findall(r"```[a-z]*\n(.*?)```", text_ or "", _re.S):
        b = " ".join(block.split())
        if b:
            out.add(b)
    for url in _re.findall(r"https?://[^\s\"'`\)\]]+", text_ or ""):
        u = url.rstrip(".,;")
        if not _LOCAL_HOST_RE.match(u):
            out.add(u)
    return out


_VERSIONISH_RE = None


def _url_skeleton(url: str) -> tuple:
    """§17.883 — a URL's identity modulo version guessing: (host,
    frozenset(path segments with version-ish segments masked)). The live
    guess-cycle: releases/latest/download/R.tar.gz → download/v5.3.3/… →
    download/v5.3.0/… — three 'different' URLs, one failing endpoint family.
    Masking version segments and ignoring order makes them EQUAL."""
    global _VERSIONISH_RE
    import re as _re
    from urllib.parse import urlparse
    if _VERSIONISH_RE is None:
        _VERSIONISH_RE = _re.compile(r"^(v?\d[\w.\-]*|latest|master|main|stable|current)$", _re.I)
    try:
        p = urlparse(url)
        segs = frozenset(
            "~V~" if _VERSIONISH_RE.match(s) else s.lower()
            for s in p.path.split("/") if s
        )
        return (p.netloc.lower(), segs)
    except Exception:
        return ("", frozenset([url]))


# §17.906 — commands that only READ state. Re-running one is never a "repeat"
# worth blocking: diagnose-first is the behaviour the escalation directive is
# actively asking for, so flagging `qm config 106` as a repeat would fight the
# very fix it is meant to enable. First token after an optional `sudo`, plus the
# verb+subcommand pairs whose mutating siblings share a binary (`qm config` is
# read-only, `qm set` is not).
_READONLY_VERBS = frozenset({
    "ls", "cat", "less", "head", "tail", "grep", "egrep", "rg", "find", "stat",
    "df", "du", "free", "lsblk", "blkid", "lscpu", "lsmod", "lspci", "lsusb",
    "ip", "ping", "ss", "netstat", "ps", "top", "uname", "whoami", "id",
    "which", "whereis", "echo", "printf", "pwd", "date", "uptime", "hostname",
    "env", "printenv", "journalctl", "dmesg", "true", "test", "file", "wc",
})
_READONLY_PAIRS = frozenset({
    ("qm", "config"), ("qm", "list"), ("qm", "status"), ("qm", "showcmd"),
    ("pct", "config"), ("pct", "list"), ("pct", "status"),
    ("pvesm", "list"), ("pvesm", "status"), ("pvesh", "get"),
    ("systemctl", "status"), ("systemctl", "is-active"),
    ("systemctl", "is-enabled"), ("systemctl", "list-units"),
    ("docker", "ps"), ("docker", "logs"), ("docker", "inspect"),
    ("git", "status"), ("git", "log"), ("git", "diff"), ("git", "show"),
    ("apt", "list"), ("apt", "show"), ("apt-cache", "policy"),
    # index refresh only — a near-universal prerequisite LINE, and the most
    # likely false positive now that matching is line-granular. Safe: no fix
    # ever hinges on being stopped from re-running it.
    ("apt", "update"), ("apt-get", "update"),
    ("zpool", "status"), ("zfs", "list"),
})
# A fetch that writes to disk or pipes into a shell is a real action (the live
# Radarr guess-cycle was exactly a repeated `curl -o`); a bare probe is not.
_FETCH_WRITES_RE = re.compile(r"(^|\s)(-o|-O|--output|--remote-name)(\s|=|$)|\|\s*(ba)?sh\b")


def _is_readonly_command(line: str) -> bool:
    """True when `line` only inspects state, so repeating it is legitimate.

    A compound is read-only only if EVERY segment is: `apt update` alone is an
    index refresh, but `apt update && apt install -y openssh-server` installs.
    Pipes are deliberately NOT split — `curl … | sh` must stay one segment so
    `_FETCH_WRITES_RE` sees it.
    """
    parts = [p for p in re.split(r"&&|\|\||;", line or "") if p.strip()]
    if len(parts) > 1:
        return all(_is_readonly_command(p) for p in parts)
    s = (line or "").strip()
    if not s or s.startswith("#"):
        return True  # comments/blank carry no action
    if s.startswith("$"):  # a copied `$ cmd` prompt marker, not a comment marker
        s = s[1:].strip()
    toks = s.split()
    while toks and toks[0] in ("sudo", "-E", "time", "\\"):
        toks = toks[1:]
    if not toks:
        return True
    verb = toks[0].rsplit("/", 1)[-1]
    if verb in _READONLY_VERBS:
        return True
    if len(toks) >= 2 and (verb, toks[1]) in _READONLY_PAIRS:
        return True
    if verb in ("curl", "wget"):
        return not _FETCH_WRITES_RE.search(s)
    return False


# §17.913 — idempotent LIFECYCLE commands are not remedies and cannot be
# "exhausted": you stop and start a VM many times in one troubleshooting run.
# Live, the caution banner read "repeats something already tried on this step
# that did not resolve it (`qm stop 106`)" — technically true, useless as a
# warning, and it is stapled to the top of a fix whose actual content was
# correct. A gate that cries wolf on `qm stop` teaches the operator to skip the
# banner that also carries the real repeats.
_LIFECYCLE_RE = re.compile(
    r"^(?:sudo\s+)?(?:qm|pct)\s+(?:start|stop|shutdown|reboot|reset|suspend|resume)\s"
    r"|^(?:sudo\s+)?systemctl\s+(?:start|stop|restart|reload)\s"
    r"|^(?:sudo\s+)?(?:docker|virsh)\s+(?:start|stop|restart)\s"
    r"|^(?:sudo\s+)?reboot\b|^(?:sudo\s+)?shutdown\b",
    re.IGNORECASE,
)


def _is_lifecycle_command(line: str) -> bool:
    """True for a start/stop/restart that is legitimately repeated."""
    return bool(_LIFECYCLE_RE.match((line or "").strip()))


_HEREDOC_START_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _command_lines_only(block: str) -> list[str]:
    """Non-blank lines of a fenced block with heredoc BODIES removed.

    The line that OPENS the heredoc is kept — it carries the real command
    (`cat > /path <<'EOF'`). The body and its terminator are dropped. An
    unterminated heredoc swallows the rest of the block, which is the
    conservative direction: under-indexing costs a missed repeat, over-indexing
    puts file content in the operator's face as a false "already tried".
    """
    lines = (block or "").splitlines()
    kept: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        if not ln.strip():
            continue
        kept.append(ln)
        m = _HEREDOC_START_RE.search(ln)
        if not m:
            continue
        delim = m.group(2)
        while i < len(lines):
            body = lines[i]
            i += 1
            # The terminator may carry trailing shell punctuation when the
            # heredoc is nested inside a quoted `bash -c "…"` — live: `EOF"`.
            if body.strip().rstrip("\"'`);") == delim:
                break
    return kept


def _command_corpus(text_: str, *, fenced: bool) -> set[str]:
    """Normalized commands from a walkthrough (`fenced=True`) or from a raw
    newline/blank-line separated failed-command blob (`fenced=False`).

    §17.906 — indexes BOTH whole blocks and their individual mutating lines.
    Block-only granularity was the hole that made the whole gate inert: a fix
    that re-prescribed one already-failed line inside a fresh 3-line block
    matched nothing.
    """
    out: set[str] = set()
    if fenced:
        blocks = re.findall(r"```[a-z]*\n(.*?)```", text_ or "", re.S)
    else:
        blocks = re.split(r"\n\s*\n", text_ or "")
    for block in blocks:
        lines = [ln for ln in (block or "").splitlines() if ln.strip()]
        if not lines:
            continue
        whole = " ".join(block.split())
        # A block of pure diagnostics is exempt wholesale.
        if whole and not all(
                _is_readonly_command(ln) or _is_lifecycle_command(ln) for ln in lines):
            out.add(whole)
        if len(lines) > 1:
            # §17.956 — the text a heredoc WRITES is data, not commands. The
            # whole-block signature above still covers the full text (so two
            # different files written to the same path stay different actions);
            # only the per-LINE index skips the body.
            for ln in _command_lines_only(block):
                if _is_readonly_command(ln) or _is_lifecycle_command(ln):
                    continue
                norm = " ".join(ln.split())
                if norm:
                    out.add(norm)
    return out


_ROOT_SHELL_RE = re.compile(r"\broot@|\bas root\b", re.IGNORECASE)


# §17.1056 — a line that hands its command to ANOTHER machine. The
# missing-tools ledger describes the operator's own shell; what runs after
# `ssh host`, `pct exec N --`, `qm guest exec`, `docker exec` runs elsewhere.
# Live: `ssh aedefruscio@192.168.1.127 nvidia-smi` (the VM) was footnoted
# "nvidia-smi is not installed on this box — install it first" (the host).
_REMOTE_TARGET_RE = re.compile(
    r"^(?:sudo\s+)?(?:ssh|pct\s+(?:exec|enter)|qm\s+guest\s+exec|docker\s+(?:exec|run)|"
    r"podman\s+(?:exec|run)|kubectl\s+exec|lxc\s+exec|incus\s+exec|vagrant\s+ssh)\b")


def runs_on_another_host(line: str) -> bool:
    return bool(_REMOTE_TARGET_RE.match((line or "").strip()))


def find_unavailable_tools(text_out: str, missing: list | None) -> list[dict]:
    """Commands in the draft that invoke a tool this session has PROVEN absent.

    Returns ``[{tool, line}]``. Word-boundary matched inside fenced blocks only,
    so prose discussing the tool is never flagged. Lines that run their
    command on another host (§17.1056) are never flagged.
    """
    names = [str(m.get("tool") or "").strip() for m in (missing or [])
             if isinstance(m, dict) and str(m.get("tool") or "").strip()]
    if not names or not (text_out or "").strip():
        return []
    hits: list[dict] = []
    seen: set[str] = set()
    for block in re.findall(r"```[a-z]*\n(.*?)```", text_out, re.S):
        for raw in block.splitlines():
            line = " ".join(raw.split())
            if not line or line.startswith("#") or runs_on_another_host(line):
                continue
            for tool in names:
                if re.search(rf"(?:^|[|&;]\s*|\s){re.escape(tool)}\s", line + " "):
                    key = f"{tool}::{line}"
                    if key not in seen:
                        seen.add(key)
                        hits.append({"tool": tool, "line": line[:160]})
    return hits


def operator_is_root(environment: dict | None) -> bool:
    """§17.913 — does the session's execution context run as root?"""
    profile = str((environment or {}).get("profile") or "")
    return bool(_ROOT_SHELL_RE.search(profile))


# Must catch `sudo` after a separator too — the live command was
# `sudo lvextend … && sudo resize2fs …` and a line-anchored rule stripped only
# the first, leaving a command that still died. Applied ONLY inside fenced
# blocks: a prose sentence like "you need sudo for this" must survive intact.
_SUDO_IN_CMD_RE = re.compile(r"(?m)(^|&&|\|\||;|\|)([ \t]*)sudo[ \t]+(?=\S)")


# §17.922 — WHICH MACHINE does this block run on? A draft routinely spans two:
# the Proxmox host (root, no sudo) and a guest console (an ordinary user, where
# sudo is REQUIRED). §17.913 decided this by matching prose in the preceding
# 400 chars and missed six of eight real phrasings — "Run these commands in the
# VM:", "From the VM shell:", "Once logged into Ubuntu, run:" — so `sudo` was
# stripped from GUEST commands, which then fail permission-denied. That is the
# operator's report: "it not giving sudo within the console commands".
#
# Prose matching was the wrong instrument. The engine already emits a STRUCTURED
# location banner on every walkthrough (§17.700/741):
#     📍 On: the Proxmox host shell (root@pve)
#     📍 In: the Proxmox VM 106 Console (noVNC)
#     📍 On: VM 106 shell (ubuntu@palworld-server)
# Use the nearest PRECEDING banner as the authority, and when there is none, do
# NOT strip: leaving a needless sudo is recoverable and obvious, silently
# removing a required one is neither.
_LOCATION_BANNER_RE = re.compile(r"📍\s*(?:On|In|At)\s*:?\s*([^\n]{0,120})",
                                 re.IGNORECASE)
_ROOT_HOST_BANNER_RE = re.compile(
    r"\broot@|\bproxmox\s+host\b|\bhost\s+shell\b|\bpve\s+shell\b|\bhypervisor\b",
    re.IGNORECASE,
)
_GUEST_BANNER_RE = re.compile(
    r"\bconsole\b|\bnovnc\b|\bguest\b|\b(?:vm|ct)\s*\d{2,5}\s+shell\b"
    r"|[a-z][a-z0-9_-]*@(?!pve\b)[a-z0-9_-]+",
    re.IGNORECASE,
)
# Fallback for a block that has no 📍 banner above it — which includes the
# 👉 "Do this next" block, since the banner follows it. Every alternative below
# is a phrasing observed in a real live walkthrough during §17.922-924 testing;
# the first cut missed six of eight and the second still missed "Log into the VM
# via the Proxmox Web UI Console".
_IN_GUEST_MARKER_RE = re.compile(
    r"inside the (?:vm|guest|container)"
    r"|(?:vm|guest|ui)\s+console"
    r"|console\s+(?:for|of|button|tab)\b"
    r"|(?:web\s*ui|proxmox)[^.\n]{0,40}console"
    r"|in the guest|in the vm\b|from the vm\b|on the vm\b"
    r"|vm shell|guest shell|novnc"
    r"|log\s?g?in(?:to|ged)?\s+(?:to\s+|into\s+)?the\s+(?:vm|guest|ubuntu)"
    r"|logged\s+(?:in)?\s*(?:to|into)\s+(?:the\s+)?(?:vm|ubuntu|guest)"
    r"|on the ubuntu (?:server|vm|guest|prompt)"
    r"|login prompt"
    r"|[a-z][a-z0-9_-]*@(?!pve\b)[a-z0-9_-]+:~",
    re.IGNORECASE,
)
_GUEST_LOOKBEHIND = 400


def _block_runs_on_root_host(text_: str, block_start: int) -> bool:
    """True only when the nearest preceding 📍 banner names the ROOT HOST.

    Conservative by construction: no banner, or an ambiguous one, returns False
    so nothing is stripped.
    """
    head = text_[:block_start]
    banners = list(_LOCATION_BANNER_RE.finditer(head))
    if banners:
        last = banners[-1]
        where = last.group(1)
        if _GUEST_BANNER_RE.search(where):
            return False          # a guest context — sudo is legitimate
        if _ROOT_HOST_BANNER_RE.search(where):
            # A host banner does NOT own every block below it: a walkthrough
            # commonly banners the host once and then moves into the guest in
            # prose ("Now run these inside the VM Console"). Prose AFTER the
            # banner overrides it — caught by test_in_guest_blocks_keep_their_sudo,
            # where the guest block inherited the host banner and lost its sudo.
            if _IN_GUEST_MARKER_RE.search(head[last.end():]):
                return False
            return True
        return False              # an ambiguous banner: do not strip
    # No banner anywhere above (the 👉 headline block precedes its own banner).
    # Strip ONLY when the prose positively identifies the host; an
    # unclassifiable block is left alone. A needless `sudo` fails visibly with
    # "sudo: command not found" — which the engine now knows about and can
    # correct — while a silently removed one fails permission-denied inside a
    # guest, which is what the operator actually hit.
    tail = head[-_GUEST_LOOKBEHIND:]
    if _IN_GUEST_MARKER_RE.search(tail):
        return False
    return bool(_ROOT_HOST_BANNER_RE.search(tail))


def _strip_sudo_in_fences(text_: str) -> tuple[str, int]:
    """Drop `sudo` from fenced blocks that run on the ROOT host only."""
    total = 0

    def _fix_block(m: "re.Match") -> str:
        nonlocal total
        if not _block_runs_on_root_host(text_, m.start()):
            return m.group(0)
        body, n = _SUDO_IN_CMD_RE.subn(r"\1\2", m.group(2))
        total += n
        return m.group(1) + body + m.group(3)

    out = re.sub(r"(```[a-z]*\n)(.*?)(```)", _fix_block, text_ or "", flags=re.S)
    return out, total


# §17.924 — a console block the operator must TYPE, emitted as a chain. Live:
# the body of a fix correctly listed `sudo apt update` / `sudo apt install -y
# qemu-guest-agent` on separate lines AND said "remember: no copy-paste in this
# window", while its own 👉 headline said
#     apt update && apt install -y qemu-guest-agent
# — 46 characters, chained, and missing the sudo its own steps used. The prompt
# rule reached the body and not the headline, which is the §17.668 house rule
# yet again: enforce it.
#
# Splitting is a SAFE repair for a typed workflow: the operator types one line,
# sees its result, then types the next — which is what the walkthrough's own
# prose already tells them to do. `&&`'s stop-on-failure is preserved by the
# human, who can see the failure.
_PRIVILEGED_GUEST_RE = re.compile(
    r"^\s*(?:apt|apt-get|dpkg|snap|systemctl|lvextend|lvresize|resize2fs|"
    r"pvresize|mkfs\S*|mount|umount|usermod|useradd|groupadd|chown|chmod|"
    r"ufw|timedatectl|hostnamectl)\b",
    re.IGNORECASE,
)


_HISTORY_EVENT_TRIGGER = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-!?#{^"


def _line_has_history_event(line: str) -> str | None:
    """The first `!` on this line bash would expand, or None."""
    in_single = False
    i = 0
    while i < len(line):
        ch = line[i]
        if ch == "\\" and not in_single:
            i += 2                      # \! is literal inside double quotes
            continue
        if ch == "'":
            in_single = not in_single
            i += 1
            continue
        if ch == "!" and not in_single and i + 1 < len(line):
            if line[i + 1] in _HISTORY_EVENT_TRIGGER:
                return line[i:i + 12]
        i += 1
    return None


def _history_scannable_lines(block: str) -> list[str]:
    """Lines of a block that an interactive bash reads as COMMAND text.

    The distinction decides this whole gate, and it is not the obvious one.
    Measured under a pty:

      * `cat > f <<'EOF'` … `EOF` — a real heredoc to the outer shell. Its body
        is NOT history-expanded. `x !id y` passes through untouched.
      * `bash -c "cat > f <<'EOF'` … `EOF"` — the `<<'EOF'` is INSIDE an open
        double quote, so the outer shell never opens a heredoc at all; it is
        reading a multi-line quoted string, and every continuation line goes
        through history expansion. This is the live T33 construct, and it
        reproduces the operator's `bash: !id: event not found` exactly.

    So a heredoc body is skipped only when the heredoc genuinely belongs to the
    outer shell — i.e. its `<<` was not itself inside quotes.
    """
    lines = (block or "").splitlines()
    out: list[str] = []
    in_s = in_d = False
    i = 0
    while i < len(lines):
        ln = lines[i]
        i += 1
        out.append(ln)
        delim = None
        j = 0
        while j < len(ln):
            c = ln[j]
            if c == "\\":
                j += 2
                continue
            if c == "'" and not in_d:
                in_s = not in_s
            elif c == '"' and not in_s:
                in_d = not in_d
            elif not in_s and not in_d and ln.startswith("<<", j):
                m = _HEREDOC_START_RE.match(ln, j)
                if m:
                    delim = m.group(2)
                    j = m.end()
                    continue
            j += 1
        if delim and not in_s and not in_d:
            while i < len(lines):                 # a true heredoc: body is safe
                body = lines[i]
                i += 1
                if body.strip().rstrip("\"'`);") == delim:
                    out.append(body)
                    break
    return out


def _dquote_continuation_lines(block: str) -> set[int]:
    """Indexes of lines that begin inside an open double-quoted string."""
    inside: set[int] = set()
    in_s = in_d = False
    for idx, ln in enumerate(block.splitlines()):
        if in_d and not in_s:
            inside.add(idx)
        j = 0
        while j < len(ln):
            c = ln[j]
            if c == "\\":
                j += 2
                continue
            if c == "'" and not in_d:
                in_s = not in_s
            elif c == '"' and not in_s:
                in_d = not in_d
            j += 1
    return inside


def _escape_expansions(line: str) -> tuple[str, int]:
    """Backslash-escape `$` and backticks the outer shell would act on."""
    out: list[str] = []
    fixed = 0
    j = 0
    while j < len(line):
        c = line[j]
        if c == "\\" and j + 1 < len(line):
            out.append(line[j:j + 2])
            j += 2
            continue
        if c in "$`":
            # A lone `$` before whitespace or end-of-line is inert to the shell.
            if c == "$" and (j + 1 >= len(line) or line[j + 1] in " \t\"'"):
                out.append(c)
                j += 1
                continue
            out.append("\\" + c)
            fixed += 1
            j += 1
            continue
        out.append(c)
        j += 1
    return "".join(out), fixed


def repair_unescaped_expansions(text_out: str) -> tuple[str, list[str]]:
    """Escape what the outer shell would otherwise substitute into the file."""
    if not (text_out or "").strip():
        return text_out, []
    total = 0

    def _fix(m: "re.Match") -> str:
        nonlocal total
        body = m.group(2)
        lines = body.splitlines(keepends=True)
        targets = _dquote_continuation_lines(body)
        if not targets:
            return m.group(0)
        out: list[str] = []
        for idx, raw in enumerate(lines):
            if idx not in targets:
                out.append(raw)
                continue
            stripped = raw.rstrip("\n")
            tail = raw[len(stripped):]
            fixed, n = _escape_expansions(stripped)
            total += n
            out.append(fixed + tail)
        return f"```{m.group(1)}\n{''.join(out)}```"

    out_text = re.sub(r"```([a-z]*)\n(.*?)```", _fix, text_out, flags=re.S)
    if not total:
        return text_out, []
    return out_text, [
        f"escaped {total} `$`/backtick character(s) inside a quoted heredoc so "
        "the outer shell writes them to the file instead of substituting them"
    ]


_PASTE_SAFE_BYTES = 1200


def _heredoc_write_parts(body: str):
    """Split a fenced block into (prefix, open_line, body_lines, term, suffix).

    Returns None when the block is not a single heredoc file write, which is
    the only shape this rewrite knows how to reassemble safely.
    """
    lines = body.splitlines()
    open_idx = None
    delim = None
    for i, ln in enumerate(lines):
        m = _HEREDOC_START_RE.search(ln)
        if m:
            open_idx, delim = i, m.group(2)
            break
    if open_idx is None:
        return None
    term_idx = None
    for j in range(open_idx + 1, len(lines)):
        if lines[j].strip().rstrip("\"'`);") == delim:
            term_idx = j
            break
    if term_idx is None:
        return None
    return (lines[:open_idx], lines[open_idx], lines[open_idx + 1:term_idx],
            lines[term_idx], lines[term_idx + 1:])


def _append_form(open_line: str) -> str | None:
    """`cat > FILE` → `cat >> FILE`, so chunks 2..N extend the file."""
    out, n = re.subn(r"(?<![>\d])>(?![>])", ">>", open_line, count=1)
    return out if n else None


def _verify_command_for(open_line: str) -> str | None:
    """Turn the write into a byte-count check the operator can compare."""
    head = open_line.split("<<", 1)[0].rstrip()
    if not head:
        return None
    head, n = re.subn(r"\bcat\s+>>?\s*", "wc -c ", head, count=1)
    if not n:
        return None
    if head.count('"') % 2:          # the heredoc lived inside the quotes
        head += '"'
    return head


_DEDUPE_MIN_BYTES = 400


_WRITE_PATH_RE = re.compile(
    r"""\b(?:cat|tee)\s*(?P<append>>>?)\s*"?(?P<path>(?:/|\./|~/)[^\s"'`;|&)]+)""")


def dedupe_repeated_file_writes(text_out: str) -> tuple[str, list[str]]:
    """Collapse a later re-print of a file already written earlier in the reply."""
    if not (text_out or "").strip():
        return text_out, []
    seen: dict[tuple[str, str], bool] = {}
    notes: list[str] = []

    def _fix(m: "re.Match") -> str:
        whole, body = m.group(0), m.group(2)
        if len(whole) < _DEDUPE_MIN_BYTES:
            return whole
        parts = _heredoc_write_parts(body)
        if not parts:
            return whole
        _prefix, open_line, body_lines, _term, _suffix = parts
        pm = _WRITE_PATH_RE.search(open_line)
        if not pm or pm.group("append") == ">>":
            return whole          # an append is a chunk, never a duplicate
        # Indentation is the only difference between the two live copies, so it
        # cannot be part of the identity.
        key = (pm.group("path"),
               "\n".join(ln.strip() for ln in body_lines if ln.strip()))
        if key not in seen:
            seen[key] = True
            return whole
        notes.append(
            f"removed a second full copy of `{pm.group('path')}` from this "
            "reply — it was the same file printed twice")
        return (f"*(the same `cat > {pm.group('path')}` block as above — "
                "run it once, not twice.)*")

    out = re.sub(r"```([a-z]*)\n(.*?)```", _fix, text_out, flags=re.S)
    return (out, notes) if notes else (text_out, [])


def split_large_paste_blocks(text_out: str) -> tuple[str, list[str]]:
    """Chunk oversized heredoc writes into paste-safe appends."""
    if not (text_out or "").strip():
        return text_out, []
    notes: list[str] = []

    def _fix(m: "re.Match") -> str:
        lang, body = m.group(1), m.group(2)
        if len(m.group(0)) <= _PASTE_SAFE_BYTES:
            return m.group(0)
        parts = _heredoc_write_parts(body)
        if not parts:
            notes.append(
                f"this block is {len(m.group(0))} characters — paste it in "
                "pieces if your terminal drops any of it")
            return m.group(0)
        prefix, open_line, body_lines, term, suffix = parts
        appender = _append_form(open_line)
        if not appender:
            return m.group(0)

        overhead = len(open_line) + len(term) + 2
        chunks: list[list[str]] = [[]]
        size = overhead
        for ln in body_lines:
            if chunks[-1] and size + len(ln) + 1 > _PASTE_SAFE_BYTES:
                chunks.append([])
                size = overhead
            chunks[-1].append(ln)
            size += len(ln) + 1
        if len(chunks) < 2:
            return m.group(0)

        total = sum(len(ln.encode("utf-8")) + 1 for ln in body_lines)
        out: list[str] = []
        for k, chunk in enumerate(chunks, 1):
            head = prefix if k == 1 else []
            opener = open_line if k == 1 else appender
            hint = ("" if k > 1 else
                    " If the prompt turns into `>` instead of coming back, the "
                    "paste was clipped — press Ctrl-C and tell me.")
            out.append(
                f"**Paste {k} of {len(chunks)}** — wait for the prompt to come "
                f"back before the next one.{hint}\n\n"
                f"```{lang}\n" + "\n".join(head + [opener] + chunk + [term])
                + "\n```")
        verify = _verify_command_for(open_line)
        if verify:
            out.append(
                f"**Then confirm it arrived intact** — this must print "
                f"`{total}`. Any other number means a paste was clipped; tell "
                f"me the number and we redo that piece.\n\n"
                f"```{lang}\n{verify}\n```")
        if suffix:
            out.append(f"```{lang}\n" + "\n".join(suffix) + "\n```")
        notes.append(
            f"split a {len(m.group(0))}-character block into {len(chunks)} "
            "pastes with a byte-count check, because a block this size arrives "
            "truncated in most terminals")
        return "\n\n".join(out)

    return re.sub(r"```([a-z]*)\n(.*?)```", _fix, text_out, flags=re.S), notes


_NONTERMINATING_FIXES: list[tuple[str, str, str]] = [
    # (pattern, replacement, human explanation)
    (r"\bpm2\s+logs\b(?![^\n]*--nostream)", r"pm2 logs --nostream",
     "`pm2 logs` tails forever; `--nostream` prints and exits"),
    (r"\btail\s+-[fF]\b", "tail -n 50",
     "`tail -f` never exits; `-n 50` prints the same tail and returns"),
    # The follow flag has to be REMOVED, not merely preceded by a bound —
    # `journalctl -n 50 --no-pager -u nginx -f` still follows.
    (r"\bjournalctl\b([^\n]*?)\s+(?:-f|--follow)\b",
     r"journalctl -n 50 --no-pager\1",
     "`journalctl -f` follows forever; `-n 50 --no-pager` prints and returns"),
    (r"\bdocker\s+logs\s+(?:-f|--follow)\b", "docker logs --tail 50",
     "`docker logs -f` follows forever; `--tail 50` prints and returns"),
    (r"\bkubectl\s+logs\s+(?:-f|--follow)\b", "kubectl logs --tail=50",
     "`kubectl logs -f` follows forever; `--tail=50` prints and returns"),
    (r"\bwatch\s+(?:-n\s*\d+\s+)?", "",
     "`watch` re-runs forever; the command is run once instead"),
    (r"\b(?:htop|top)\b(?![^\n]*-b)", "top -b -n 1",
     "`top` is a full-screen program; `-b -n 1` prints one snapshot"),
    # `-c1` (no space) is as bounded as `-c 1`; missing it rewrote a correct
    # command into `ping -c 4 -c1 …`. Live text in this session used `-c1`.
    (r"\bping\b(?![^\n]*\s-c\s*\d)", "ping -c 4",
     "`ping` runs until stopped; `-c 4` sends four and returns"),
]
# Paged output: not hung, but captive until `q`.
_PAGER_FIXES: list[tuple[str, str, str]] = [
    (r"\bsystemctl\s+status\b(?![^\n]*--no-pager)", "systemctl status --no-pager",
     "`systemctl status` opens a pager; `--no-pager` prints and returns"),
    (r"\bjournalctl\b(?![^\n]*(?:--no-pager|\s-f\b|\s--follow\b))",
     "journalctl --no-pager",
     "`journalctl` opens a pager; `--no-pager` prints and returns"),
    (r"\bgit\s+(?!--no-pager)(?=log\b|diff\b|show\b)", "git --no-pager ",
     "`git log`/`diff` open a pager; `--no-pager` prints and returns"),
]
# No flag saves these — name them and say how to get out.
#
# Matched on the EFFECTIVE VERB, never by scanning the line. Scanning produced
# two false positives immediately, on real output from this very session:
# `\b(less|more|man)\b` fired on the prose "If you want more details", and
# `node\s*$` fired on `pct exec 111 -- ln -sf /usr/local/bin/node /bin/node`,
# whose verb is `ln`. A banner that cries wolf on an English sentence is the
# §17.913 mistake again.
_RUNNER_PREFIX_RE = re.compile(
    r"^\s*(?:sudo\s+|time\s+|nohup\s+|env\s+\S+=\S+\s+"
    r"|pct\s+exec\s+\S+\s+--\s+|qm\s+guest\s+exec\s+\S+\s+--\s+"
    r"|docker\s+exec\s+(?:-\S+\s+)*\S+\s+|kubectl\s+exec\s+\S+\s+--\s+"
    r"|ssh\s+\S+\s+|bash\s+-c\s+[\"']|sh\s+-c\s+[\"'])+",
    re.IGNORECASE,
)


_BLOCKING_VERBS = {
    "nano": ("an editor", "Ctrl-X"),
    "vi": ("an editor", "`:q!`"),
    "vim": ("an editor", "`:q!`"),
    "emacs": ("an editor", "Ctrl-X Ctrl-C"),
    "less": ("a pager", "`q`"),
    "more": ("a pager", "`q`"),
    "man": ("a pager", "`q`"),
    "tcpdump": ("a capture that runs until stopped", "Ctrl-C"),
}
_REPL_VERBS = {"python", "python3", "node", "irb", "psql", "mysql", "sqlite3"}


def _effective_verb(line: str) -> str:
    """The program actually being invoked, past any runner prefix."""
    stripped = _RUNNER_PREFIX_RE.sub("", (line or "").strip())
    toks = stripped.split()
    return toks[0].rsplit("/", 1)[-1].strip("\"'") if toks else ""


def _blocking_program(line: str) -> tuple[str, str] | None:
    verb = _effective_verb(line)
    if not verb:
        return None
    if verb in _BLOCKING_VERBS:
        return _BLOCKING_VERBS[verb]
    rest = _RUNNER_PREFIX_RE.sub("", line.strip()).split()[1:]
    # Only a BARE invocation is a REPL. `node -v`, `python3 -c "…"` and
    # `node server.js` all print and exit — live, `pct exec 111 -- node -v`
    # was flagged as an interactive shell the operator would be trapped in.
    if verb in _REPL_VERBS and not rest:
        return ("an interactive REPL", "`exit()` / Ctrl-D")
    if verb in ("npm", "yarn", "pnpm"):
        words = [a for a in rest if not a.startswith("-")]
        if words and words[0] == "run":
            words = words[1:]
        if words and words[0] in ("dev", "start", "serve"):
            return ("a dev server that runs until stopped", "Ctrl-C")
    if verb in ("nc", "netcat") and "-l" in rest:
        return ("a listener that runs until stopped", "Ctrl-C")
    return None


def _at_command_position(line: str, start: int) -> bool:
    """Is this match where a COMMAND begins, rather than mid-sentence?

    `top`, `watch` and `more` are ordinary English words. Live, the prose "On
    top of that, the DNS server..." was flagged as a full-screen program. A
    rewrite gate that edits sentences is worse than no gate.
    """
    head = line[:start]
    if not head.strip():
        return True
    m = _RUNNER_PREFIX_RE.match(line)
    if m and start <= m.end():
        return True
    return bool(re.search(r"(?:[|;&]|&&|\|\||--|\bif\b|\bthen\b|\bdo\b)\s*$", head))


def find_nonterminating_commands(text_out: str) -> list[dict]:
    """Prescribed commands that will not return the operator to a prompt."""
    hits: list[dict] = []
    for m in re.finditer(r"```[a-z]*\n(.*?)```", text_out or "", re.S):
        for raw in _command_lines_only(m.group(1)):   # §17.956 — not file content
            ln = raw.strip()
            if not ln or ln.startswith("#"):
                continue
            for pat, _repl, why in (_NONTERMINATING_FIXES + _PAGER_FIXES):
                m2 = re.search(pat, ln)
                if m2 and _at_command_position(ln, m2.start()):
                    hits.append({"line": ln[:120], "why": why, "fixable": True})
                    break
            else:
                blocked = _blocking_program(ln)
                if blocked:
                    hits.append({"line": ln[:120],
                                 "why": f"{blocked[0]} — leave it with {blocked[1]}",
                                 "fixable": False})
    return hits


def repair_nonterminating_commands(text_out: str) -> tuple[str, list[str]]:
    """Give every prescribed command a way back to the prompt."""
    if not (text_out or "").strip():
        return text_out, []
    notes: list[str] = []
    seen: set[str] = set()

    def _fix_block(m: "re.Match") -> str:
        body = m.group(2)
        keep = set(_command_lines_only(body))     # never touch heredoc content
        out_lines = []
        for raw in body.splitlines():
            ln = raw
            if raw in keep and raw.strip() and not raw.strip().startswith("#"):
                for pat, repl, why in (_NONTERMINATING_FIXES + _PAGER_FIXES):
                    m3 = re.search(pat, ln)
                    if not (m3 and _at_command_position(ln, m3.start())):
                        continue
                    new_ln, n = re.subn(pat, repl, ln, count=1)
                    if n:
                        ln = re.sub(r"\s{2,}", " ", new_ln).rstrip()
                        if why not in seen:
                            seen.add(why)
                            notes.append(why)
                        break
            out_lines.append(ln)
        return f"```{m.group(1)}\n" + "\n".join(out_lines) + "\n```"

    out = re.sub(r"```([a-z]*)\n(.*?)```", _fix_block, text_out, flags=re.S)
    # Blocking programs cannot be rewritten — warn instead.
    for h in find_nonterminating_commands(out):
        if not h["fixable"] and h["why"] not in seen:
            seen.add(h["why"])
            notes.append(f"`{h['line'][:60]}` starts {h['why']}")
    return (out if out != text_out else text_out), notes


def find_history_expansion_hazards(text_out: str) -> list[dict]:
    """Fenced blocks carrying a `!` an interactive bash would expand."""
    hits: list[dict] = []
    for m in re.finditer(r"```[a-z]*\n(.*?)```", text_out or "", re.S):
        for raw in _history_scannable_lines(m.group(1)):
            tok = _line_has_history_event(raw)
            if tok:
                hits.append({"line": " ".join(raw.split())[:120], "token": tok})
                break
    return hits


def repair_history_expansion(text_out: str) -> tuple[str, list[str]]:
    """Prefix `set +H` to any fenced block bash would mangle on paste."""
    if not (text_out or "").strip():
        return text_out, []
    repaired = 0

    def _fix(m: "re.Match") -> str:
        nonlocal repaired
        body = m.group(2)
        if not any(_line_has_history_event(ln)
                   for ln in _history_scannable_lines(body)):
            return m.group(0)
        if body.lstrip().startswith("set +H"):
            return m.group(0)
        repaired += 1
        return f"```{m.group(1)}\nset +H\n{body}```"

    out = re.sub(r"```([a-z]*)\n(.*?)```", _fix, text_out, flags=re.S)
    if not repaired:
        return text_out, []
    return out, [
        "prefixed `set +H` because this block contains `!`, which an "
        "interactive bash would otherwise read as a history reference "
        "(`event not found`) before the command ever runs"
    ]


def repair_console_commands(text_out: str) -> tuple[str, list[str]]:
    """Split chained commands in CONSOLE blocks and report privileged guest
    commands that are missing `sudo`. Returns ``(text, notes)``."""
    if not (text_out or "").strip():
        return text_out, []
    notes: list[str] = []
    split_count = 0
    missing_sudo: list[str] = []

    def _fix(m: "re.Match") -> str:
        nonlocal split_count
        if _block_runs_on_root_host(text_out, m.start()):
            return m.group(0)          # host shell — pasteable, leave alone
        out_lines: list[str] = []
        for raw in m.group(2).splitlines():
            line = raw.rstrip()
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                parts = [p.strip() for p in re.split(r"&&", stripped) if p.strip()]
                if len(parts) > 1:
                    split_count += len(parts) - 1
                    out_lines.extend(parts)
                else:
                    out_lines.append(stripped)
            else:
                out_lines.append(line)
        for ln in out_lines:
            if (_PRIVILEGED_GUEST_RE.match(ln) and not ln.lower().startswith("sudo ")
                    and ln not in missing_sudo):
                missing_sudo.append(ln)
        return m.group(1) + "\n".join(out_lines) + "\n" + m.group(3)

    out = re.sub(r"(```[a-z]*\n)(.*?)\n?(```)", _fix, text_out, flags=re.S)
    if split_count:
        notes.append(
            f"split {split_count} chained command(s) into separate lines — the "
            "VM console has no copy-paste, so each is typed by hand")
    if missing_sudo:
        notes.append(
            "these console commands need `sudo` (a guest console is an ordinary "
            "user, unlike the Proxmox host): `"
            + "`, `".join(c[:48] for c in missing_sudo[:3]) + "`")
    return out, notes


def repair_unavailable_tools(text_out: str, environment: dict | None) -> tuple[str, list[str]]:
    """§17.913 — make the draft runnable on the box the operator actually has.

    Returns ``(text, notes)``.

    * ``sudo`` on a ROOT shell is REMOVED, not flagged. Root does not need it and
      Proxmox (Debian minimal) does not ship it, so the prefix is pure breakage:
      live, `sudo lvextend …` died with `sudo: command not found` twice.
      Stripping is safe by construction — every command runs with strictly the
      same privileges.
    * Any OTHER proven-missing tool gets a note saying how to obtain it, because
      "what the operator has available and how to attain it" is the actual
      question; silently emitting a command they cannot run is not an answer.
    """
    if not (text_out or "").strip():
        return text_out, []
    missing = [str(m.get("tool") or "").strip()
               for m in ((environment or {}).get("missing_tools") or [])
               if isinstance(m, dict) and str(m.get("tool") or "").strip()]
    missing_l = {t.lower() for t in missing}
    notes: list[str] = []
    out = text_out

    if "sudo" in missing_l or operator_is_root(environment):
        fixed, n = _strip_sudo_in_fences(out)
        if n:
            out = fixed
            why = ("this shell has no `sudo`" if "sudo" in missing_l
                   else "you are already root")
            notes.append(f"dropped `sudo` from {n} command(s) — {why}")

    for tool in missing:
        if tool.lower() == "sudo":
            continue
        if find_unavailable_tools(out, [{"tool": tool}]):
            notes.append(
                f"`{tool}` is not installed on this box — install it first with "
                f"`apt-get install -y {tool}`, or use an alternative")
    return out, notes


def unavailable_tools_note(notes: list[str]) -> str:
    """Operator-facing footer for what was repaired or is still missing."""
    if not notes:
        return ""
    return ("\n\n---\nℹ️ **Adjusted for your system:** "
            + "; ".join(notes) + ".")


# §17.919 — the walkthrough asserts the step's OWN GOAL is already achieved.
# §17.917 added the invariant to the guide prompt ("This step is NOT done …")
# and VERIFIED it reaches the model — and the very next draw still opened with
# "change the boot order to prioritize the virtual hard disk where Ubuntu was
# installed" and "the login prompt asking for the username of the account you
# created during installation", for a step titled "Install Ubuntu Server 22.04
# on VM 106" that the operator had never completed. Prompt rules are guidance;
# this is enforcement (§17.668/882, the house rule).
#
# High precision by construction: it fires only when the STEP'S OWN action verb
# is asserted in the completed past. Forward-looking uses ("once installed",
# "after the installation completes", "will be installed") are excluded — those
# are how a correct walkthrough talks about its own outcome.
# §17.921 — recall gap found live: a draft said "installation has been run at
# least once" and "the password created during the installation", neither of
# which contains the participle of the step's verb. Any COMPLETION participle
# applied to the step's own noun counts.
_GENERIC_DONE_RE = re.compile(
    r"[^.!?\n]*\b(?:has|have|was|were)\s+been\s+"
    r"(?:run|performed|completed|done|carried out)\b[^.!?\n]*"
    r"|[^.!?\n]*\b(?:created|configured|installed|set up)\s+during\s+"
    r"(?:the\s+)?(?:install|installation|setup|configuration)\b[^.!?\n]*",
    re.IGNORECASE,
)
_STEP_ACTIONS: dict[str, tuple[str, ...]] = {
    "install": ("installed",),
    "create": ("created",),
    "configure": ("configured",),
    "deploy": ("deployed",),
    "provision": ("provisioned",),
    "set up": ("set up", "setup"),
}
_FORWARD_LOOKING_RE = re.compile(
    r"\b(?:once|after|when|until|before|if)\b[^.!?\n]{0,40}$", re.IGNORECASE)


def find_presupposed_completion(text_out: str, title: str) -> list[dict]:
    """Sentences asserting the step's own goal is already done.

    Returns ``[{action, sentence}]``.
    """
    if not (text_out or "").strip() or not (title or "").strip():
        return []
    tl = title.lower()
    actions = [a for a in _STEP_ACTIONS if a in tl]
    if not actions:
        return []
    hits: list[dict] = []
    seen: set[str] = set()
    for action in actions:
        for past in _STEP_ACTIONS[action]:
            # "was/were/has been/have been <past>" or "you <past> during …"
            pat = re.compile(
                r"[^.!?\n]*\b(?:(?:was|were|has\s+been|have\s+been|is|are)"
                r"\s+(?:successfully\s+|already\s+)?" + re.escape(past) + r"\b"
                r"|already\s+" + re.escape(past) + r"\b"
                r"|you\s+\w+\s+during\s+(?:the\s+)?" + re.escape(action) + r"\w*\b"
                r")[^.!?\n]*", re.IGNORECASE)
            for m in pat.finditer(text_out):
                sentence = " ".join(m.group(0).split())
                if not sentence or sentence in seen:
                    continue
                # a clause introduced by once/after/when is about the FUTURE
                head = text_out[max(0, m.start() - 40):m.start()]
                if _FORWARD_LOOKING_RE.search(head):
                    continue
                seen.add(sentence)
                hits.append({"action": action, "sentence": sentence[:200]})
    # §17.921 — completion phrasings that never name the step's verb.
    for m in _GENERIC_DONE_RE.finditer(text_out):
        sentence = " ".join(m.group(0).split())
        if sentence and sentence not in seen:
            head = text_out[max(0, m.start() - 40):m.start()]
            if _FORWARD_LOOKING_RE.search(head):
                continue
            seen.add(sentence)
            hits.append({"action": "completion", "sentence": sentence[:200]})
    return hits


# §17.925 — GOAL DRIFT. A step carries its own definition of done, and nothing
# measured progress against it. Live (session 613dd1df, ADD5): the step reads
#
#   "The step is complete when the VM boots from its local disk into a working
#    login prompt and is reachable via SSH from the Proxmox host."
#
# The operator reported the login prompt at turn 1460 — half the criteria met.
# The remaining one is SSH. The engine then spent SIX CONSECUTIVE turns on
# `qemu-guest-agent`, which appears nowhere in the criteria, and never once
# proposed `openssh-server`. Each individual reply was locally reasonable (the
# guest agent genuinely is missing; the apt cdrom error genuinely needs fixing),
# and the sequence went nowhere — the operator's report: "the continuation is
# just a repeat that will not achieve anything nor solve the problem".
#
# This is an unbounded yak-shave: solve whatever error is in front of you,
# forever, without asking whether it advances the step.
_ACCEPTANCE_RE = re.compile(
    r"(?:the\s+)?step\s+is\s+complete\s+when\b[^.]*\.?"
    r"|(?:is\s+)?(?:considered\s+)?done\s+when\b[^.]*\.?"
    r"|success(?:\s+is|\s+means|:)\s*[^.]*\.?"
    r"|acceptance\s+criteri(?:a|on)\s*:?\s*[^.]*\.?",
    re.IGNORECASE,
)
_CRITERIA_STOPWORDS = frozenset({
    "the", "step", "is", "complete", "when", "and", "a", "an", "to", "of", "on",
    "in", "with", "from", "into", "its", "it", "be", "been", "via", "for", "or",
    "that", "this", "are", "was", "were", "has", "have", "done", "working",
    "successfully", "should", "must", "can", "will", "then", "at", "by",
})


def extract_acceptance_criteria(description: str | None) -> str:
    """The step's own definition of done, verbatim. "" when it states none."""
    m = _ACCEPTANCE_RE.search(description or "")
    return " ".join(m.group(0).split()) if m else ""


def _criteria_terms(criteria: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9][a-z0-9._-]{2,}", (criteria or "").lower())
            if w not in _CRITERIA_STOPWORDS}


def find_goal_drift(recent_assistant_texts: list[str], criteria: str,
                    *, window: int = 4) -> bool:
    """True when the last `window` replies advanced none of the criteria.

    Deliberately crude: a reply "advances" a criterion if it mentions any
    distinctive term from it. That is a low bar, and the live failure cleared
    it in neither direction — six consecutive replies about a package the
    criteria never name.
    """
    terms = _criteria_terms(criteria)
    if not terms:
        return False
    recent = [t for t in (recent_assistant_texts or []) if (t or "").strip()][:window]
    if len(recent) < window:
        return False          # too early to call it drift
    return not any(
        any(term in (t or "").lower() for term in terms) for t in recent
    )


def find_guess_before_look(text_out: str) -> list[dict]:
    """Fix drafts that mutate state without first showing the operator a
    read-only command that prints it. Returns ``[{command}]`` naming the
    offending first mutation, or ``[]`` when a discovery step comes first.

    Deliberately permissive: ANY read-only command anywhere before the first
    mutation satisfies it. This asks the engine to look, not to look well.
    """
    ordered: list[str] = []
    for block in re.findall(r"```[a-z]*\n(.*?)```", text_out or "", re.S):
        for raw in (block or "").splitlines():
            ln = " ".join((raw or "").split())
            if ln and not ln.startswith("#"):
                ordered.append(ln)
    for ln in ordered:
        if _is_readonly_command(ln):
            return []       # it looked first — satisfied
        return [{"command": ln}]  # first actionable line already mutates
    return []               # no commands at all (prose answer) — nothing to gate


def _text_without_heredoc_bodies(text_: str) -> str:
    """The text with every fenced block's heredoc BODY removed.

    Used only for URL extraction. Commands keep their own path through
    `_command_corpus`/`_command_lines_only`; this is the same rule applied to
    the two gates that read the raw text instead.
    """
    if not (text_ or "").strip():
        return text_ or ""

    def _strip(m: "re.Match") -> str:
        kept = _command_lines_only(m.group(2))
        return f"```{m.group(1)}\n" + "\n".join(kept) + "\n```"

    return re.sub(r"```([a-z]*)\n(.*?)```", _strip, text_, flags=re.S)


def find_repeated_failed(text_out: str, failed_commands: str) -> list[str]:
    """§17.882/883/906 — deterministic repeat detection: which already-failed
    commands or URLs does this new walkthrough prescribe AGAIN — exactly, as an
    individual line lifted into a new block, or as a version-guess VARIATION of
    the same failing endpoint family? The §17.882 exact matcher blocked
    identical repeats and the model responded by mutating the version tag three
    times; prompt rules are guidance, this is enforcement.

    §17.906 — the command half of this gate had been DEAD since §17.882. The
    old line wrapped the whole `\n\n`-joined failed-command list in ONE fence,
    so `_normalized_commands` collapsed every failed command into a single
    whitespace-joined blob; set-membership then could not match any individual
    command, and the gate returned [] whenever more than one command had failed
    — i.e. in every real troubleshooting session. Live cost (session
    613dd1df/T23): `qm destroy 106 --purge` prescribed FOUR times, with no
    warning banner, across a 27-hour Ubuntu-install loop. The URL half kept
    working (URLs are regex-extracted individually), which is why the gate
    looked healthy.
    """
    import re as _re
    if not (text_out or "").strip() or not (failed_commands or "").strip():
        return []
    new_cmds = _command_corpus(text_out, fenced=True)
    old_cmds = _command_corpus(failed_commands, fenced=False)
    # URLs stay matched individually and independently of command granularity.
    # §17.961 — URLs from a file's CONTENTS are data, not prescriptions.
    _stripped = _text_without_heredoc_bodies(text_out)
    _url_cmds = {u for u in _normalized_commands(_stripped) if u.startswith("http")}
    new_cmds |= _url_cmds
    old_urls_raw = {
        u.rstrip(".,;")
        for u in _re.findall(r"https?://[^\s\"\'`\)\]]+", failed_commands or "")
        if not (_LOCAL_HOST_RE and _LOCAL_HOST_RE.match(u))
    }
    old_cmds |= old_urls_raw
    hits = {c for c in new_cmds if c in old_cmds}
    # §17.883 — version-masked skeleton match on URLs only.
    # §17.961 — over commands only: a template literal in a file the operator is
    # writing must not drag the whole write into the banner as a URL repeat.
    old_urls = {u for u in old_cmds if u.startswith("http")}
    old_skels = {_url_skeleton(u) for u in old_urls}
    for c in (_command_corpus(_stripped, fenced=True) | _url_cmds):
        for u in _re.findall(r"https?://[^\s\"'`\)\]]+", c):
            u = u.rstrip(".,;")
            if _url_skeleton(u) in old_skels and c not in hits:
                hits.add(c)
    return sorted(hits)


_NEXT_ACTION_SECTION_RE = re.compile(r"##\s*👉[^\n]*\n(.*?)(?=\n##\s|\Z)", re.S)


def _primary_action_corpus(draft: str) -> set[str]:
    """Normalized commands from the fix's single immediate action.

    The §17.741 directive mandates a leading `## 👉 Do this next` section whose
    one fenced block is the thing to run right now; prefer it, and fall back to
    the draft's first fenced block. An empty set means the action could not be
    located, which the caller reads as "stay loud".
    """
    m = _NEXT_ACTION_SECTION_RE.search(draft or "")
    region = m.group(1) if m else (draft or "")
    fences = re.findall(r"```[a-z]*\n.*?```", region, re.S)
    if not fences:
        return set()
    return _command_corpus(fences[0], fenced=True)


def _repeats_in_primary_action(draft: str, hits: list[str]) -> list[str]:
    """Keep only the repeats the operator is being told to run RIGHT NOW."""
    if not hits:
        return hits
    primary = _primary_action_corpus(draft)
    if not primary:
        return hits
    url_hits = {h for h in hits if "http" in h}
    kept = {h for h in hits if "http" not in h and h in primary}
    return sorted(kept | url_hits)


_CONSUMING_MARKERS = (" -o ", " -O", "wget ", "| sh", "| bash", "|sh", "|bash",
                      "git clone", "dpkg -i", "apt install", "apt-get install",
                      "pip install", "sh -c", "> /", "tee /")


def find_novel_urls(text_out: str, grounding_corpus: str) -> list[str]:
    """§17.883 — external URLs the draft tells the operator to CONSUME (download
    to disk, pipe to a shell, install from) that appear NOWHERE in its
    grounding (research block, playbook, conversation, the operator's own
    pasted output, the step task). A consumed URL with no provenance is a
    GUESS — the root disease behind today's cycles (radarr.video, then three
    invented GitHub version tags). READ-ONLY inspection URLs (a `curl -s` API
    query whose output the operator pastes back) are exempt — discovery is
    self-verifying and is exactly the behavior the gate's regeneration
    directive demands; flagging it (the first live proof-run did) would punish
    the cure. Local/RFC1918 URLs are exempt (the operator's own services)."""
    global _LOCAL_HOST_RE
    import re as _re
    if not (text_out or "").strip():
        return []
    _normalized_commands("")  # ensure _LOCAL_HOST_RE is built
    corpus = grounding_corpus or ""
    novel: list[str] = []
    for block in _re.findall(r"```[a-z]*\n(.*?)```", text_out, _re.S):
        flat = " ".join(block.split())
        if not any(m in flat for m in _CONSUMING_MARKERS):
            continue  # read-only / discovery command — exempt
        for url in _re.findall(r"https?://[^\s\"'`\)\]]+", block):
            u = url.rstrip(".,;")
            if _LOCAL_HOST_RE.match(u):
                continue
            if u not in corpus and u.rstrip("/") not in corpus and u not in novel:
                novel.append(u)
    return novel


def find_banned_values(text_out: str, banned: list | None) -> list[dict]:
    """§17.893 — deterministic banned-value detection. `banned` is the
    session's ``environment.banned_values`` list of {value, reason}: concrete
    identifiers the operator has explicitly ruled out for new use (live
    incident: 'DarthSidious' is the HP SWITCH's hostname; with the §17.892 pin
    removed and the constraint verbatim IN the prompt, the model still copied
    the name from the prior walkthrough in its conversation window into
    `qm create --name` — §17.882's lesson again: prompts are guidance, this is
    enforcement). Word-boundary, case-insensitive; values under 3 chars are
    ignored (too collision-prone to enforce)."""
    import re as _re
    if not (text_out or "").strip() or not banned:
        return []
    hits: list[dict] = []
    for b in banned:
        v = str((b or {}).get("value") or "").strip()
        if len(v) < 3:
            continue
        if _re.search(rf"(?<![\w-]){_re.escape(v)}(?![\w-])", text_out, _re.I):
            hits.append({"value": v, "reason": str((b or {}).get("reason") or "").strip()})
    return hits


# ── §17.898 — VM-vs-container resource-kind gate ─────────────────────────
#
# Proxmox addresses a VM with `qm` and an LXC container with `pct`, by numeric
# ID. Using the wrong verb is always an error, and the engine committed it live:
# facts recorded "VM 106 (palworld-server)", yet the guide prescribed `pct enter
# 106` three times across an ask and two guides. The session's other five
# resources (102-105) really are containers, so the container-shaped context
# out-voted the one fact that mattered. The confirmed facts are ground truth and
# this makes them binding — the §17.893 lesson: prompts are guidance,
# enforcement is code.
_VM_FACT_RE = None
_CT_FACT_RE = None
_VM_CMD_RE = None
_CT_CMD_RE = None


def resource_kinds_from_facts(environment: Optional[dict]) -> dict[str, str]:
    """Map Proxmox resource id → ``'vm'`` / ``'ct'`` from the confirmed facts.

    An id the facts describe BOTH ways is ambiguous and is dropped: a
    half-remembered fact must never become an enforcement rule."""
    global _VM_FACT_RE, _CT_FACT_RE
    import re as _re
    if _VM_FACT_RE is None:
        # "VM 106", "VM 106 (palworld-server)" — but not "VM/LXC 106".
        _VM_FACT_RE = _re.compile(r"(?<![\w/])VM\s+(\d{2,5})\b")
        # "container 103", "LXC container 104", "CT 107".
        _CT_FACT_RE = _re.compile(r"(?<![\w/])(?:LXC\s+)?(?:container|CT)\s+(\d{2,5})\b",
                                  _re.I)
    kinds: dict[str, str] = {}
    conflict: set[str] = set()
    for fact in (environment or {}).get("facts") or []:
        s = str(fact or "")
        for rid in _VM_FACT_RE.findall(s):
            if kinds.setdefault(rid, "vm") != "vm":
                conflict.add(rid)
        for rid in _CT_FACT_RE.findall(s):
            if kinds.setdefault(rid, "ct") != "ct":
                conflict.add(rid)
    for rid in conflict:
        kinds.pop(rid, None)
    return kinds


def find_resource_kind_violations(
    text_out: str, kinds: dict[str, str] | None,
) -> list[dict]:
    """§17.898 — fenced commands that address a resource with the WRONG verb.

    Returns ``[{id, used, correct, command}]``. Only fenced blocks are scanned:
    prose legitimately says "`pct` is for containers" while explaining the very
    mistake this gate catches, and flagging that would punish the correction."""
    global _VM_CMD_RE, _CT_CMD_RE
    import re as _re
    if not (text_out or "").strip() or not kinds:
        return []
    if _VM_CMD_RE is None:
        # `qm start 106`, `qm set 106 --…`, `qm resize 106 scsi0 +60G`
        _VM_CMD_RE = _re.compile(r"\bqm\s+(?:[a-z][\w-]*\s+)?(\d{2,5})\b")
        _CT_CMD_RE = _re.compile(r"\bpct\s+(?:[a-z][\w-]*\s+)?(\d{2,5})\b")
    hits: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for block in _re.findall(r"```[a-z]*\n(.*?)```", text_out, _re.S):
        for line in block.splitlines():
            for rx, used in ((_VM_CMD_RE, "vm"), (_CT_CMD_RE, "ct")):
                for rid in rx.findall(line):
                    want = kinds.get(rid)
                    if want and want != used and (rid, used) not in seen:
                        seen.add((rid, used))
                        hits.append({
                            "id": rid, "used": used, "correct": want,
                            "command": line.strip()[:120],
                        })
    return hits


def _kind_word(k: str) -> str:
    return "a VM (use `qm`)" if k == "vm" else "an LXC container (use `pct`)"


def resource_kind_warning(hits: list[dict]) -> str:
    """§17.898 — the visible flag when a redraw still addresses a resource with
    the wrong verb (the §17.883 honesty contract: gates guarantee VISIBILITY,
    not correctness)."""
    if not hits:
        return ""
    return ("\n\n---\n⚠️ **Wrong resource type:** this reply uses "
            + "; ".join(
                f"`{h['used']}` commands for {h['id']}, which your confirmed "
                f"facts record as {_kind_word(h['correct'])}"
                for h in hits[:3])
            + ". Do not run those commands as written.")


def banned_values_warning(hits: list[dict]) -> str:
    """§17.893 — the visible flag when a redraw still carries a banned value
    (the §17.883 honesty contract: gates guarantee VISIBILITY, not
    correctness)."""
    if not hits:
        return ""
    return ("\n\n---\n⚠️ **Reserved value:** this walkthrough uses "
            + "; ".join(f"`{h['value']}`" + (f" ({h['reason']})" if h["reason"] else "")
                        for h in hits[:3])
            + " — the operator has ruled this value out for new use. Substitute "
              "your own value everywhere it appears before running anything.")


# §17.927 — a conclusion that is not an action leaves the operator parked.
# Live (session 613dd1df, turn 1497), the ENTIRE walkthrough for ADD3:
#
#   "The operator has explicitly requested to remove this step from the project
#    plan. No further action is required for the Markdown linter implementation."
#
# The engine had assessed the step correctly — ADD3/ADD4 are junk steps created
# from casual test messages ("I want to build a markdown linter") and really are
# obsolete — and then did nothing with that assessment. No skip, no advance, no
# offer. The operator, who had just finished ADD5, was handed a two-sentence
# dead end. Their report: "this appears to be its ability to move on after
# completing the task".
#
# §17.915 already draws the distinction this needs: SKIP is "work deliberately
# NOT done", which is exactly the right terminal state for an obsolete step, and
# unlike a commit it requires no evidence. So the fix is to turn the conclusion
# into the one-word action that acts on it.
_NO_ACTION_RE = re.compile(
    r"no (?:further |additional )?action (?:is |was )?(?:required|needed)"
    r"|nothing (?:further |more )?(?:to do|is required|is needed)"
    r"|(?:requested to |should be )?remove(?:d)? (?:this step )?from the (?:project )?plan"
    r"|this step (?:is (?:no longer|not) (?:required|needed|relevant)|can be skipped)"
    r"|already (?:been )?(?:complete|completed|done|achieved)\b",
    re.IGNORECASE,
)


# §17.937 — a walkthrough claiming the PLAN ITSELF was changed. Distinct from
# _NO_ACTION_RE, which catches "there is nothing to do here": this catches an
# assertion of COMPLETED SYSTEM STATE — "the plan has been updated", "this step
# has been removed". The engine cannot change the plan by saying so; only a
# skip/drop does. Live cost (session 613dd1df, node ADD3): asked to write a
# walkthrough for a step the operator wanted gone, the model replied "The
# project plan has been updated to remove this step. No further action is
# required." — FOUR times across five days, 2026-08-31 to 09-04, while the node
# sat `pending` in dag_nodes the whole time. §17.927 dutifully appended "reply
# `skip` to retire this step" underneath, but the operator had just been told
# the step was already gone, so the offer read as noise and the step stayed in
# the plan.
_PLAN_MUTATION_CLAIM_RE = re.compile(
    r"\b(?:the\s+)?(?:project\s+)?plan\s+(?:has\s+been|was|is\s+now)\s+"
    r"(?:updated|changed|revised|modified|amended)\b"
    r"|\bthis\s+step\s+(?:has\s+been|was|is\s+now)\s+"
    r"(?:removed|deleted|dropped|retired|taken\s+out)\b"
    r"|\b(?:has\s+been|was)\s+removed\s+from\s+the\s+(?:project\s+)?plan\b"
    r"|\bi\s+(?:have\s+)?(?:removed|deleted|dropped|retired)\s+this\s+step\b",
    re.IGNORECASE,
)


def claims_plan_mutation(text_out: str) -> bool:
    """§17.937 — does this walkthrough assert the plan was already changed?"""
    return bool(_PLAN_MUTATION_CLAIM_RE.search(text_out or ""))


def concludes_no_action_required(text_out: str) -> bool:
    """True when a walkthrough's conclusion is that the step needs no work.

    Scoped to SHORT replies: a long walkthrough that merely mentions something
    is already done in passing is still a walkthrough. A reply that is mostly
    this conclusion is a dead end unless it carries an action.
    """
    t = (text_out or "").strip()
    if not t or len(t) > 1200:
        return False
    return bool(_NO_ACTION_RE.search(t))


# §17.932 — a walkthrough that never names its finish line leaves the operator
# reporting output forever. The directive asks the model for a "Done when"
# close; this is the enforcement half, because a prompt rule is a request and
# the live transcript is full of ignored ones. Matching is deliberately loose:
# any heading or bolded lead-in that states a completion condition counts, so a
# model that phrases it its own way is not double-footered.
_DONE_WHEN_RE = re.compile(
    r"(?:^|\n)\s*(?:#{1,4}\s*|\*\*)\s*(?:✅\s*)?"
    r"(?:done\s+when|step\s+is\s+(?:done|complete)|"
    r"you(?:'?re| are)\s+done\s+when|success\s+looks\s+like|"
    r"how\s+you(?:'?ll| will)\s+know)\b",
    re.IGNORECASE,
)


def has_done_criterion(text_out: str) -> bool:
    """§17.932 — does this walkthrough already state an observable finish line?"""
    return bool(_DONE_WHEN_RE.search(text_out or ""))


def extract_done_criterion(text_out: str) -> str:
    """§17.1100 — the walkthrough's own 'Done when' block, verbatim: from the
    header to the next top-level heading (or end). This is the exact bar the
    operator was told to hit, and the completion verifier judges against it so a
    paste that meets it auto-commits instead of getting 'I couldn't verify'."""
    t = text_out or ""
    m = _DONE_WHEN_RE.search(t)
    if not m:
        return ""
    start = m.start()
    # stop at the next top-level (## or ---) section after the header
    nxt = re.search(r"\n\s*(?:#{1,4}\s|\s*---\s*\n)", t[m.end():])
    end = m.end() + nxt.start() if nxt else len(t)
    return t[start:end].strip()
