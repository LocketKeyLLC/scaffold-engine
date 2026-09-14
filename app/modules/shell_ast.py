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
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_INTERPRETERS = {"sh", "bash", "zsh", "dash", "ksh", "python", "python3", "python2", "perl", "ruby", "node"}
_SCRIPT_FLAGS = {"-c", "-e"}
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


def analyze(cmd: str, *, _depth: int = 0) -> ShellFacts:
    facts = ShellFacts()
    src = (cmd or "").encode("utf-8")
    if not src.strip():
        return facts
    try:
        tree = _get_parser().parse(src)
    except Exception:  # noqa: BLE001 — a missing grammar is a parse error for the gate
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
                    argv.append(_unquote(raw))
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
                if argv[0] in _INTERPRETERS:
                    for i, a in enumerate(argv[1:], 1):
                        if a in _SCRIPT_FLAGS and i + 1 < len(argv):
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
                target = _unquote(_text(ch, src))
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
