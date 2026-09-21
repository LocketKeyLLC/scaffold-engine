"""§17.1159 — a paste is parsed ONCE, deterministically, into what the
operator typed and what the machine printed; every consumer reads the pairs.

Before this, one prompt regex decided "this is a shell paste", and then the
verifier, the fix flow, the decide layer and the fact scribe each re-read
the raw text with the model — four chances to attribute an output to the
wrong command, miss which one failed, or read a config-file body as an
error. The state check never had that problem because its probes carry
``== id ==`` markers and are attributed deterministically. This module gives
every other paste the same footing:

- ``parse_paste`` → ``Paste`` (entries of command → output, host/user from
  the prompt, heredoc bodies attached to their opener, the shell's exit code
  when the paste shows one, the trailing prompt that proves the last command
  returned, a hung continuation prompt, and the step SENTINEL);
- ``match_block`` → which of the issued block's commands ran, were skipped,
  were edited, and what extra was run;
- ``shape_guard`` → the plain sentence for the three paste shapes that
  silently break judgments: a hung ``>`` prompt, commands with no output and
  no returned prompt (still running), the block copied back without running it;
- ``sentinel_for``/``block_hash`` → ``== S:<step>/<hash8> ==``, appended by the
  SPA's copy button as an ``echo`` so the paste attributes itself to the step
  AND the exact block it came from, and proves the block ran to its end.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Optional

# A shell prompt at the start of a line: user@host:path# / [user@host path]$ /
# PS C:\...> / a bare "$ ". The trailing space is required so "$HOME" is not a prompt.
_PROMPT_RE = re.compile(
    r"^\s*(?:\[?(?P<user>[A-Za-z_][\w.-]*)@(?P<host>[\w.-]+)[^$#>\n]*\]?\s*(?P<sym>[$#>])|PS\s+[A-Z]:\\[^>\n]*(?P<ps>>)|(?P<bare>\$))\s?(?P<cmd>.*)$")
_BARE_PROMPT_RE = re.compile(r"^\s*(?:\[?[A-Za-z_][\w.-]*@[\w.-]+[^$#>\n]*\]?\s*[$#]|PS\s+[A-Z]:\\[^>\n]*>|\$)\s*$")
_CONTINUATION_RE = re.compile(r"^\s*>\s?\S")
_HEREDOC_RE = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?")
_RC_RE = re.compile(r"^\s*rc=(\d+)\s*$")
SENTINEL_RE = re.compile(r"^\s*==\s*S:(?P<step>[A-Za-z0-9_.-]+)/(?P<hash>[0-9a-f]{8})\s*==\s*$", re.M)
_ERROR_RE = re.compile(r"(?i)\b(?:error|failed|failure|cannot|can't|could not|no such file|not found|permission denied|"
                       r"refused|unreachable|no route|timed out|timeout|denied|invalid|fatal|exception|traceback|E:|W:)\b")


@dataclass
class Entry:
    command: str
    output: str = ""
    host: Optional[str] = None
    user: Optional[str] = None
    heredoc: bool = False
    exit_code: Optional[int] = None

    @property
    def errored(self) -> bool:
        if self.exit_code is not None:
            return self.exit_code != 0
        return bool(_ERROR_RE.search(self.output or "")) and not self.heredoc


@dataclass
class Paste:
    entries: list[Entry] = field(default_factory=list)
    prompt_seen: bool = False
    trailing_prompt: bool = False        # the paste ends on a bare prompt → the last command returned
    continuation: bool = False           # a hung `>` continuation prompt: the shell is waiting for more input
    sentinel_step: Optional[str] = None  # == S:<step>/<hash> == seen
    sentinel_hash: Optional[str] = None
    sentinel_at_end: bool = False        # the sentinel is the LAST thing printed → the block ran to its end
    leading_output: str = ""             # printed lines before any prompt (a paste that starts mid-output)

    @property
    def hosts(self) -> list[str]:
        out: list[str] = []
        for e in self.entries:
            if e.host and e.host not in out:
                out.append(e.host)
        return out

    @property
    def commands(self) -> list[str]:
        return [e.command for e in self.entries if e.command]

    @property
    def failed(self) -> list[Entry]:
        return [e for e in self.entries if e.errored]


def _norm(cmd: str) -> str:
    return " ".join((cmd or "").strip().split())


def block_hash(block: str) -> str:
    """8 hex chars over the normalised block (lines stripped, blanks and
    comments dropped) — the SAME function lives in app/ui/static/util.js
    (`blockHash`); tests/test_assist_paste.py asserts parity on a fixture."""
    lines = [ln.strip() for ln in strip_sentinel_lines(block or "").splitlines()]
    lines = [ln for ln in lines if ln and not ln.startswith("#")]
    h = 0x811C9DC5
    for ch in "\n".join(lines).encode("utf-8"):
        h ^= ch
        h = (h * 0x01000193) & 0xFFFFFFFF
    return f"{h:08x}"


def sentinel_for(step: str, block: str) -> str:
    return f'echo "== S:{step}/{block_hash(block)} =="'


def strip_sentinel_lines(block: str) -> str:
    """The block without its own sentinel echo (so hashing is stable whether
    or not the sentinel was appended)."""
    return "\n".join(ln for ln in (block or "").splitlines() if not re.match(r'^\s*echo\s+"== S:[^"]+ =="\s*$', ln))


def parse_paste(text: str) -> Paste:
    p = Paste()
    cur: Optional[Entry] = None
    terminator: Optional[str] = None
    heredoc_body: list[str] = []
    lines = (text or "").splitlines()
    last_nonblank = ""
    for raw in lines:
        line = raw.rstrip("\r")
        if line.strip():
            last_nonblank = line
        if terminator is not None:                       # inside a heredoc body → belongs to the opener, not output
            if line.strip() == terminator:
                terminator = None
            continue
        sm = SENTINEL_RE.match(line)
        if sm:
            p.sentinel_step, p.sentinel_hash = sm.group("step"), sm.group("hash")
            if cur is not None:
                cur.output = (cur.output + "\n" + line).strip("\n")
            continue
        if _BARE_PROMPT_RE.match(line):                   # a prompt with nothing typed → the previous command returned
            p.prompt_seen = True
            cur = None
            continue
        m = _PROMPT_RE.match(line)
        if m and m.group("cmd") is not None and (m.group("user") or m.group("ps") or m.group("bare")):
            cmd = m.group("cmd").strip()
            if not cmd:
                p.prompt_seen = True
                cur = None
                continue
            p.prompt_seen = True
            cur = Entry(command=cmd, host=m.group("host"), user=m.group("user"))
            hm = _HEREDOC_RE.search(cmd)
            if hm:
                terminator = hm.group(1)
                cur.heredoc = True
            p.entries.append(cur)
            continue
        if _CONTINUATION_RE.match(line) and cur is not None and not cur.output:
            # a continuation line of a multi-line command (`> ...`): part of the command, not output
            cur.command += " " + line.strip()[1:].strip()
            continue
        rm = _RC_RE.match(line)
        if rm and cur is not None:
            cur.exit_code = int(rm.group(1))
            continue
        if cur is not None:
            cur.output = (cur.output + "\n" + line).strip("\n") if cur.output else line
        else:
            p.leading_output = (p.leading_output + "\n" + line).strip("\n") if p.leading_output else line
    p.trailing_prompt = bool(_BARE_PROMPT_RE.match(last_nonblank)) if last_nonblank else False
    # a hung continuation prompt: the last non-blank line is a lone `>` (the shell wants more input)
    p.continuation = bool(re.match(r"^\s*>\s*$", last_nonblank)) or (
        sum(1 for ln in lines[-3:] if re.match(r"^\s*>\s*$", ln)) >= 1 and not p.trailing_prompt)
    if p.sentinel_step:
        tail = [ln for ln in lines if ln.strip() and not _BARE_PROMPT_RE.match(ln)]
        p.sentinel_at_end = bool(tail) and bool(SENTINEL_RE.match(tail[-1]))
    return p


def render_pairs(p: Paste, *, max_chars: int = 6000, max_output_lines: int = 40) -> str:
    """The pairs as text for a prompt: numbered, command then its output
    (truncated per command, the tail kept — errors live at the end)."""
    parts: list[str] = []
    if p.leading_output:
        parts.append("(output before the first prompt)\n" + "\n".join(p.leading_output.splitlines()[-12:]))
    for i, e in enumerate(p.entries, 1):
        who = f"{e.user}@{e.host}" if e.host else "shell"
        out_lines = (e.output or "").splitlines()
        if len(out_lines) > max_output_lines:
            out_lines = ["…(earlier output truncated)…"] + out_lines[-max_output_lines:]
        body = "\n".join("    " + ln for ln in out_lines) if out_lines else "    (no output)"
        tag = ""
        if e.exit_code is not None:
            tag = f"  [exit {e.exit_code}]"
        elif e.heredoc:
            tag = "  [wrote a file via heredoc — body omitted]"
        parts.append(f"[{i}] {who}$ {e.command}{tag}\n{body}")
    txt = "\n".join(parts)
    if len(txt) > max_chars:
        txt = "…(earlier commands truncated)…\n" + txt[-max_chars:]
    return txt


def issued_commands(block: str) -> list[str]:
    """The commands an issued block asks for: one per non-empty, non-comment
    line, minus the sentinel echo; a `\\`-continued line is one command."""
    out: list[str] = []
    buf = ""
    for ln in strip_sentinel_lines(block or "").splitlines():
        s = ln.strip()
        if not s or s.startswith("#"):
            continue
        if s.endswith("\\"):
            buf += s[:-1] + " "
            continue
        out.append(_norm(buf + s))
        buf = ""
    if buf.strip():
        out.append(_norm(buf))
    return out


def match_block(block: str, p: Paste) -> dict:
    """``{ran, skipped, edited, extra, complete}``: the issued commands that
    appear in the paste verbatim, the ones that do not, the typed commands
    that look like an edited issued command (same head + first argument),
    the typed commands not in the block at all; ``complete`` when every
    issued command ran (or the block's own sentinel closed the paste)."""
    issued = issued_commands(block)
    typed = [_norm(c) for c in p.commands if not re.match(r'^echo\s+"== S:[^"]+ =="$', _norm(c))]   # the sentinel is ours
    typed_set = set(typed)
    ran = [c for c in issued if c in typed_set]
    skipped = [c for c in issued if c not in typed_set]
    edited: list[tuple[str, str]] = []
    for c in list(skipped):
        head = " ".join(c.split()[:2])
        for t in typed:
            if t not in issued and t.split()[:2] == c.split()[:2] and head:
                edited.append((c, t))
                skipped.remove(c)
                break
    extra = [t for t in typed if t not in set(issued) and t not in {e[1] for e in edited}]
    complete = not skipped or (p.sentinel_at_end and p.sentinel_hash == block_hash(block))
    return {"ran": ran, "skipped": skipped, "edited": edited, "extra": extra, "complete": complete,
            "issued": issued}


def looks_like_block_copied_back(text: str, block: Optional[str]) -> bool:
    """The operator pasted the command block itself (no prompts, no output)
    instead of running it."""
    if not block or not (text or "").strip():
        return False
    p = parse_paste(text)
    if p.prompt_seen:
        return False
    lines = [_norm(ln) for ln in text.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    issued = issued_commands(block)
    if not lines or not issued:
        return False
    return sum(1 for ln in lines if ln in set(issued)) >= max(1, int(0.8 * len(issued))) and len(lines) <= len(issued) + 1


def shape_guard(text: str, block: Optional[str] = None) -> Optional[str]:
    """One plain sentence when the paste's SHAPE means it cannot be judged
    yet; None when it is judgeable. Deterministic; runs before any model."""
    p = parse_paste(text)
    if looks_like_block_copied_back(text, block):
        return ("That is the command block itself, not its output — it looks like it was copied back without being run. "
                "Run it in the shell on the target, then paste what it prints (the lines after each command).")
    if p.continuation:
        return ("Your shell is waiting for more input (the `>` continuation prompt): a quote or bracket in the pasted "
                "command is still open. Press Ctrl-C to get the normal prompt back, then paste the block again as one piece.")
    if p.entries and not p.trailing_prompt and not p.sentinel_at_end and not (p.entries[-1].output or "").strip():
        return (f"The last command (`{p.entries[-1].command[:80]}`) shows no output and the prompt has not come back — "
                f"it may still be running. Wait for the prompt, then paste again from that command down.")
    return None


_FENCE_RE = re.compile(r"```(?:bash|sh|shell)?[ \t]*\n(.*?)```", re.S)


def blocks_in(text: str) -> list[str]:
    return [b.strip() for b in _FENCE_RE.findall(text or "") if b.strip()]


def find_issued_block(guidance_texts: list[str], p: Paste) -> Optional[str]:
    """The block the paste came from: by the sentinel's hash when the paste
    carries one, else the block (of the most recent text first) whose issued
    commands overlap the typed ones most. None when nothing overlaps."""
    cands: list[str] = []
    for t in guidance_texts:
        cands.extend(blocks_in(t))
    if not cands:
        return None
    if p.sentinel_hash:
        for b in cands:
            if block_hash(b) == p.sentinel_hash:
                return b
    typed = {_norm(c) for c in p.commands}
    if not typed:
        return None
    best, best_n = None, 0
    for b in cands:
        n = sum(1 for c in issued_commands(b) if c in typed)
        if n > best_n:
            best, best_n = b, n
    return best


def paste_verdict(text: str, guidance_texts: list[str]) -> Optional[dict]:
    """§17.1159 — the deterministic part of judging a paste against the block
    the engine issued: ``{outcome, reason, summary, block, match, paste}``
    with outcome 'incomplete' when issued commands were not run, else
    'complete' (the model verifier then judges the OUTPUTS); None when the
    paste cannot be tied to an issued block."""
    p = parse_paste(text)
    if not p.entries:
        return None
    block = find_issued_block(guidance_texts, p)
    if not block:
        return None
    m = match_block(block, p)
    if m["complete"] or not m["skipped"]:
        return {"outcome": "complete", "block": block, "match": m, "paste": p,
                "summary": f"all {len(m['issued'])} issued command(s) ran" + (" (sentinel confirmed)" if p.sentinel_at_end else "")}
    skipped = "\n".join(f"- `{c}`" for c in m["skipped"][:6])
    edited = "".join(f"\n- `{a}` was run as `{b}`" for a, b in m["edited"][:3])
    return {"outcome": "incomplete", "block": block, "match": m, "paste": p,
            "summary": f"{len(m['skipped'])} of {len(m['issued'])} issued command(s) did not run",
            "reason": (f"**{len(m['ran'])} of {len(m['issued'])} commands from the block ran.** These did not:\n{skipped}"
                       + (f"\n\nEdited before running:{edited}" if edited else "")
                       + "\n\nRun the missing ones and paste their output, or say why they were skipped.")}
