"""§17.1067 — shell commands as structure, not text.

The state-check probe gate (`read_only_command`) and the fix-draft scanners
decided from regexes over raw text. That refuses read-only probes whose
ARGUMENTS contain a verb (`getent passwd prowlarr`, `ls /etc/apt`,
`grep 'rm -rf' /var/log/syslog` — live, six of the operator's 48 probes were
refused this way) and it cannot see structure a shell will act on
(`$(…)` substitutions, `bash -c "…"` scripts, heredocs into `tee`).

`analyze()` walks the tree-sitter-bash AST and returns every simple command's
argv (nested scripts and substitutions included), every redirect target, and
which commands are fed to an interpreter. Callers judge the HEAD of each
command, not its arguments. A parse error fails closed.

§17.1180 — the paragraph above used to say this serves "the state-check probe
gate … and the fix-draft scanners". It has exactly ONE importer
(`assist_state_check`); the fix-draft scanners still run regexes over raw text,
which is the very thing this module exists to replace. Stated as the intent it
is, so nobody reads the gap as already closed. Wiring them up is worth doing —
the AST sees `$(…)`, `bash -lc` and heredocs that a text scan cannot (§17.1171
was exactly that class) — but it is a behaviour change to the draft scanners,
not a docstring fix.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_INTERPRETERS = {"sh", "bash", "zsh", "dash", "ksh", "python", "python3", "python2", "perl", "ruby", "node"}
# §17.1171 — a script flag may ride in a SHORT-FLAG BUNDLE. `_SCRIPT_FLAGS` was
# an exact-token set, so `bash -lc '…'`, `sh -ec '…'`, `bash -xc '…'` and
# `perl -le '…'` never reached the recursion below: the payload stayed a quoted
# argument, `heads` read `['bash -lc']`, and `read_only_command` saw no mutation
# verb. Measured on the live gate before the fix — `bash -lc 'rm -rf /'` was
# ALLOWED while the control `bash -c 'rm -rf /'` was refused.
_SCRIPT_FLAG_RE = re.compile(r"^-[A-Za-z]*[ce]$|^--(?:command|eval)$")
_QUOTE_RE = re.compile(r"""^(['"])(.*)\1$""", re.S)


@dataclass
class ShellFacts:
    commands: list[list[str]] = field(default_factory=list)   # argv per simple command, nested included
    heads: list[str] = field(default_factory=list)             # per command: the leading UNQUOTED tokens (max 4), joined
    redirect_targets: list[str] = field(default_factory=list)  # file targets of >, >>, <> (not < input, not /dev/null)
    piped_to_interpreter: bool = False                         # `… | sh`, `… | bash`, `… | python3`
    nested_scripts: list[str] = field(default_factory=list)    # `bash -c "…"`, `$(…)`, heredoc bodies
    parse_error: bool = False


_parser = None


def _get_parser():
    global _parser
    if _parser is None:
        import tree_sitter_bash
        from tree_sitter import Language, Parser
        _parser = Parser(Language(tree_sitter_bash.language()))
    return _parser


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _unquote(s: str) -> str:
    m = _QUOTE_RE.match(s.strip())
    return m.group(2) if m else s


#: §17.1697 — inside "…" bash removes a backslash only before $ ` " \ and newline
_DQ_ESCAPE_RE = re.compile(r"\\([$`\"\\\n])")


def _dq_value(raw: str) -> str:
    """§17.1697 — the value of a double-quoted word: its content with bash's double-quote escapes resolved.

    Live (job e5e22b92 T13, three drafts): the in-container template renders its check as `pct exec 102 -- bash -c
    "curl -s -H \\"X-Api-Key: \\$(grep -oP … config.xml)\\" http://127.0.0.1:9696/api/v1/indexer | grep -o …"`
    (§17.1496 escapes it for that slot). The value kept `\\"` and `\\$`, the inner script parsed as `\\"X-Api-Key:`
    and `\\$`, the read failed closed, the check was dropped and the frame refused "no check at all"."""
    inner = raw[1:-1] if len(raw) >= 2 and raw[0] == raw[-1] == '"' else raw
    return _DQ_ESCAPE_RE.sub(r"\1", inner)


def _word_value(node, src: bytes) -> str:
    """§17.1572b — the VALUE of an argument word, a concatenation resolved child by child.

    Live: `pct exec 111 -- sh -c 'echo '\\''--- a ---'\\''; ls /opt'` — the standard idiom for a single
    quote inside a single-quoted script — was refused by `read_only_command` although every piece of
    the script reads. tree-sitter parses that argument as a `concatenation` (raw_string, `\\'` word,
    raw_string, …) and `_unquote` on its raw text kept the quoting verbatim; the gate then re-quoted
    the wrapper's remainder around it and the inner parse failed → fail-closed → "not read-only".
    The value is each raw_string's content, each string's content, and each bare word with its
    backslash escapes resolved — what the shell itself would hand the command."""
    if node.type == "concatenation":
        parts = []
        for ch in node.children:
            if ch.type == "raw_string":
                parts.append(_text(ch, src)[1:-1])
            elif ch.type == "string":
                parts.append(_dq_value(_text(ch, src)))                       # §17.1697
            else:
                parts.append(re.sub(r"\\(.)", r"\1", _text(ch, src)))
        return "".join(parts)
    if node.type == "string":
        return _dq_value(_text(node, src))                                   # §17.1697
    return _unquote(_text(node, src))


def analyze(cmd: str, *, _depth: int = 0) -> ShellFacts:
    facts = ShellFacts()
    src = (cmd or "").encode("utf-8")
    if not src.strip():
        return facts
    try:
        tree = _get_parser().parse(src)
    except Exception:
        facts.parse_error = True
        return facts
    root = tree.root_node
    if root.has_error:
        facts.parse_error = True

    def walk(node) -> None:
        t = node.type
        if t == "command":
            argv: list[str] = []
            head: list[str] = []
            head_open = True
            for ch in node.children:
                if ch.type in ("command_name", "word", "string", "raw_string", "concatenation", "number"):
                    raw = _text(ch, src)
                    argv.append(_word_value(ch, src))
                    # quoted arguments are DATA: they never extend the head
                    if head_open and ch.type not in ("string", "raw_string") and len(head) < 4:
                        head.append(raw)
                    else:
                        head_open = False
                elif ch.type == "file_redirect":
                    _redirect(ch)
            if argv:
                facts.commands.append(argv)
                facts.heads.append(" ".join(head) if head else argv[0])
                if argv[0].rsplit("/", 1)[-1] in _INTERPRETERS:
                    for i, a in enumerate(argv[1:], 1):
                        if _SCRIPT_FLAG_RE.match(a) and i + 1 < len(argv):
                            facts.nested_scripts.append(argv[i + 1])
        elif t == "redirected_statement":
            for ch in node.children:
                if ch.type == "file_redirect":
                    _redirect(ch)
                elif ch.type == "heredoc_redirect":
                    for hd in ch.children:
                        if hd.type == "heredoc_body":
                            facts.nested_scripts.append(_text(hd, src))
        elif t == "pipeline":
            cmds = [c for c in node.children if c.type in ("command", "redirected_statement")]
            for c in cmds[1:]:
                head = c
                while head.type == "redirected_statement" and head.children:
                    head = head.children[0]
                name = next((_text(x, src) for x in head.children if x.type == "command_name"), "")
                if name in _INTERPRETERS:
                    facts.piped_to_interpreter = True
        elif t == "command_substitution":
            inner = _text(node, src)
            inner = inner[2:-1] if inner.startswith("$(") else inner.strip("`")
            facts.nested_scripts.append(inner)
        elif t == "heredoc_body":
            facts.nested_scripts.append(_text(node, src))
        for ch in node.children:
            walk(ch)

    def _redirect(node) -> None:
        op = ""; target = ""
        for ch in node.children:
            if ch.type in (">", ">>", "<>", ">|", "&>", "&>>"):
                op = ch.type
            elif ch.type in ("word", "string", "raw_string", "concatenation", "number"):
                target = _word_value(ch, src)
        if op and target and target not in ("/dev/null",) and not re.fullmatch(r"&\d+", target):
            facts.redirect_targets.append(target)

    walk(root)
    # nested scripts are analysed recursively (bounded) and their facts folded in
    if _depth < 3:
        for script in list(facts.nested_scripts):
            sub = analyze(script, _depth=_depth + 1)
            facts.commands.extend(sub.commands)
            facts.heads.extend(sub.heads)
            facts.redirect_targets.extend(sub.redirect_targets)
            facts.piped_to_interpreter = facts.piped_to_interpreter or sub.piped_to_interpreter
            facts.parse_error = facts.parse_error or sub.parse_error
    return facts


# ── §17.1982 — a program handed to `sh` that only bash can run ─────────────
#: Live (job 0fb4ea7f CL3, 2026-11-21): `qm guest exec 106 -- sh -c '… cmp -s <(sed -e "$a\\" "$A") "$B" …'`.
#: On Debian and Ubuntu `sh` is dash, which parses the whole program first and stops at `<(` with
#: `Syntax error: "(" unexpected` -- not a line runs, so the guarded `rm` never did and the step could never work.
#: The construct is read off the tree-sitter-bash AST of the INNER program, never off text: a `[[` inside a
#: quoted word, or a nested `bash -c '…'`, is data to the sh and is not a hit.
_POSIX_SHELLS = {"sh", "dash"}
#: the commands that hand their remaining argv on as a command to run: `sh` after one of these is the shell that runs
_EXEC_WRAPPERS = {"sudo", "timeout", "nohup", "env", "nice", "ionice", "setsid", "stdbuf", "chroot", "runuser",
                  "systemd-run", "pct", "qm", "lxc-attach", "docker", "podman", "kubectl", "nerdctl", "exec"}
#: §17.1982 — a container IMAGE's `sh` may be busybox and the image may hold no bash at all (`postgres:16-alpine`),
#: so the engine cannot swap the shell there: it is refused with the remedy instead of repaired.
_IMAGE_RUNNERS = {"docker", "podman", "kubectl", "nerdctl"}
#: the `${…}` operator tokens tree-sitter-bash gives that POSIX sh does not have (`:-` `:=` `#` `%` … are their own tokens)
_EXPANSION_BASHISMS = {"//": "`${var//…}` (pattern substitution)", "/": "`${var/…}` (pattern substitution)",
                       "/#": "`${var/#…}` (pattern substitution)", "/%": "`${var/%…}` (pattern substitution)",
                       "^^": "`${var^^}` (case change)", "^": "`${var^}` (case change)",
                       ",,": "`${var,,}` (case change)", ",": "`${var,}` (case change)",
                       ":": "`${var:offset:length}` (substring)", "!": "`${!var}` (indirection)",
                       "@": "`${var@…}` (transformation)"}


def bash_only_constructs(program: str) -> list[str]:
    """§17.1982 — the bash-only constructs a program uses, each named once, in the order met; ``[]`` when it is
    POSIX sh or does not parse (a program that does not parse is another gate's finding, never guessed at here).

    Exactly the constructs dash refuses or silently mis-runs: process substitution, `[[ ]]`, the `${…}` operators
    POSIX lacks, arrays, `function name`, `&>`, `<<<`, `$'…'`, `{a..b}`, `source`."""
    src = (program or "").encode("utf-8")
    if not src.strip():
        return []
    try:
        tree = _get_parser().parse(src)
    except Exception:
        return []
    if tree.root_node.has_error:
        return []
    found: list[str] = []

    def add(what: str) -> None:
        if what not in found:
            found.append(what)

    def walk(node) -> None:
        t = node.type
        if t == "process_substitution":
            add(f"`{_text(node, src)[:2]}…)` (process substitution)")
        elif t == "test_command" and node.children and node.children[0].type == "[[":
            add("`[[ … ]]`")
        elif t == "expansion":
            for ch in node.children[1:-1]:
                if ch.type == "subscript":
                    add("`${array[…]}` (an array)")
                elif ch.type in _EXPANSION_BASHISMS:
                    add(_EXPANSION_BASHISMS[ch.type])
        elif t == "array":
            add("`name=(…)` (an array)")
        elif t == "function_definition" and node.children and node.children[0].type == "function":
            add("`function name` (the bash keyword)")
        elif t == "file_redirect" and any(ch.type in ("&>", "&>>") for ch in node.children):
            add("`&>` (both streams into one file)")
        elif t == "herestring_redirect":
            add("`<<<` (a here-string)")
        elif t == "ansi_c_string":
            add("`$'…'` (an ANSI-C string)")
        elif t == "brace_expression":
            add("`{a..b}` (a brace range)")
        elif t == "command_name" and _text(node, src) == "source":
            add("`source` (POSIX sh has only `.`)")
        for ch in node.children:
            walk(ch)

    walk(tree.root_node)
    return found


@dataclass
class ShProgram:
    program: str            # what the POSIX shell is handed: a `-c` word, a heredoc body, a written file's content
    shell: str              # the shell word as written (`sh`, `/bin/sh`, `dash`)
    start: int              # byte span of that word in the TOP-LEVEL command; -1 when it sits inside a quoted word
    end: int
    in_image: bool          # run inside a container image (`docker exec …`), whose shell the engine does not know
    how: str                # "-c", "stdin", or the written file's path


def sh_programs(cmd: str, files: dict | None = None, *, _nested: bool = False) -> list[ShProgram]:
    """§17.1982 — every program this command hands to a POSIX `sh`/`dash`: `sh -c PROG` (flags may bundle, `-ec`),
    `sh -s <<EOF` / `sh <<EOF` (the heredoc body), and `sh FILE` for a file the block writes (``files``: path →
    content). The shell must be in command position: first, after `--`, or after a command that runs its argv
    (`sudo`, `timeout`, `pct exec 106 --`, `docker exec ct`). An ssh's remote command and a `bash -c` program are
    searched too, with no span (the word is inside quotes, so only a redraft can change it)."""
    out: list[ShProgram] = []
    src = (cmd or "").encode("utf-8")
    if not src.strip():
        return out
    try:
        tree = _get_parser().parse(src)
    except Exception:
        return out
    words_t = ("command_name", "word", "string", "raw_string", "concatenation", "number")

    def visit(node, heredoc: str | None) -> None:
        if node.type == "redirected_statement":
            body = None
            for ch in node.children:
                if ch.type == "heredoc_redirect":
                    body = next((_text(hd, src) for hd in ch.children if hd.type == "heredoc_body"), body)
            for ch in node.children:
                if ch.type not in ("heredoc_redirect", "file_redirect", "herestring_redirect"):
                    visit(ch, body)
            return
        if node.type == "command":
            words = [ch for ch in node.children if ch.type in words_t]
            argv = [_word_value(w, src) for w in words]
            lead = argv[0].rsplit("/", 1)[-1] if argv else ""
            for i, a in enumerate(argv):
                if a.rsplit("/", 1)[-1] not in _POSIX_SHELLS:
                    continue
                if not (i == 0 or argv[i - 1] == "--" or lead in _EXEC_WRAPPERS):
                    continue
                j, c_flag, stdin_flag = i + 1, False, False
                while j < len(argv) and argv[j][:1] in ("-", "+") and argv[j] not in ("-", "--"):
                    short = argv[j][:1] == "-" and not argv[j].startswith("--")
                    c_flag = c_flag or (short and "c" in argv[j][1:])
                    stdin_flag = stdin_flag or (short and "s" in argv[j][1:])
                    j += 1
                span = (-1, -1) if _nested else (words[i].start_byte, words[i].end_byte)
                image = lead in _IMAGE_RUNNERS
                if c_flag and j < len(argv):
                    out.append(ShProgram(argv[j], a, *span, image, "-c"))
                elif (stdin_flag or j >= len(argv)) and heredoc:
                    out.append(ShProgram(heredoc, a, *span, image, "stdin"))
                elif j < len(argv) and files and argv[j] in files:
                    out.append(ShProgram(str(files[argv[j]] or ""), a, *span, image, argv[j]))
                break
            # an ssh's remote command and a nested `bash -c` program are a shell's input too
            if lead in ("ssh", "bash", "zsh"):
                for w, a in zip(words[1:], argv[1:], strict=True):
                    if w.type in ("string", "raw_string", "concatenation") and " " in a:
                        out.extend(sh_programs(a, files, _nested=True))
            return
        for ch in node.children:
            visit(ch, heredoc)

    visit(tree.root_node, None)
    return out
