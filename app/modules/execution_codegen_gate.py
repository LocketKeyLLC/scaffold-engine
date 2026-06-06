"""§17.428 — deterministic Python-syntax gate for CodeGen node output.

The LLM verifier (``execution_verify.VERIFY_SYSTEM``) is a lenient
presence-checker — "PASS if the output contains what the task requested,
even partially". For code that bar cannot catch output that does not even
parse. This module adds a cheap, dependency-free, deterministic check: pull
the Python fenced blocks out of a CodeGen node's output and ``ast.parse``
each one. A SyntaxError is an unambiguous failure no operator wants shipped.

Design (all pure functions — the caller in ``execute_next_node`` owns the
fail-open posture and the ``verify_status`` downgrade, mirroring the
§17.376/§17.377 validation-citation guard):

- Python-only. ``ast.parse`` is stdlib and free; other languages need
  toolchains. Non-Python fences and unlabeled fences are skipped, never
  failed — a CodeGen node emitting JS/SQL/Go is not this gate's business.
- Signature stubs pass. ``def merge(a, b): ...`` is valid Python (``...``
  is ``Ellipsis``), so the §17.374 "module, not script" outputs parse fine.
- No Python block found => no findings => pass. The gate only ever fails
  output that has a Python block AND that block does not parse.

The caller treats any exception raised here as fail-open (node passes) so a
gate malfunction can never block a node — only a genuine SyntaxError can.
"""
from __future__ import annotations

import ast
import re
from dataclasses import dataclass

# Fenced code block: ```<lang>\n<code>\n```  (lang token optional).
# Non-greedy body so it stops at the first closing fence.
_FENCE_RE = re.compile(r"```([^\n`]*)\n(.*?)```", re.DOTALL)

# Language tags we treat as Python. Anything else (including the empty
# unlabeled fence) is skipped — we only parse what is explicitly Python.
PYTHON_LANGS = frozenset({"python", "py", "python3"})


@dataclass(frozen=True)
class SyntaxFinding:
    """One failed Python fenced block."""
    block_index: int          # 1-based index among ALL fenced blocks
    lineno: int | None        # line within the block (1-based), if known
    offset: int | None        # column within the line, if known
    message: str              # SyntaxError.msg
    text: str | None          # the offending source line, if known


def extract_code_blocks(output: str) -> list[tuple[str, str]]:
    """Return ``[(lang_lowercased, code), ...]`` for every fenced block.

    The lang token is whatever follows the opening fence up to the newline,
    lowercased and stripped (empty string for an unlabeled fence).
    """
    if not output:
        return []
    blocks: list[tuple[str, str]] = []
    for m in _FENCE_RE.finditer(output):
        lang = m.group(1).strip().lower()
        code = m.group(2)
        blocks.append((lang, code))
    return blocks


def check_python_syntax(output: str) -> list[SyntaxFinding]:
    """Parse every Python fenced block; return a finding per block that fails.

    Empty list = clean (no Python blocks, or all parse). Non-Python and
    unlabeled blocks are skipped. Never raises on a SyntaxError — that is
    the expected signal and is returned as a finding.
    """
    findings: list[SyntaxFinding] = []
    for idx, (lang, code) in enumerate(extract_code_blocks(output), start=1):
        if lang not in PYTHON_LANGS:
            continue
        try:
            ast.parse(code)
        except SyntaxError as e:
            findings.append(
                SyntaxFinding(
                    block_index=idx,
                    lineno=e.lineno,
                    offset=e.offset,
                    message=e.msg or "invalid syntax",
                    text=(e.text or "").rstrip("\n") or None,
                )
            )
    return findings


def format_syntax_reason(findings: list[SyntaxFinding]) -> str:
    """Build the verifier-rejection reason fed into the W.1 retry loop.

    Names each failing block + line + the SyntaxError message so the next
    attempt's ``_format_reviewer_feedback`` block tells the model exactly
    what to fix. Returns ``""`` for an empty list (caller should not call it
    in that case).
    """
    if not findings:
        return ""
    lines = [
        "§17.428 Python-syntax gate: the generated code does not parse "
        "(ast.parse raised SyntaxError). Fixing the requested feature is "
        "not enough — the output must be syntactically valid Python.",
        "",
    ]
    for f in findings:
        loc = f"block {f.block_index}"
        if f.lineno is not None:
            loc += f", line {f.lineno}"
            if f.offset is not None:
                loc += f", col {f.offset}"
        snippet = f"  >>> {f.text}" if f.text else ""
        lines.append(f"- {loc}: {f.message}")
        if snippet:
            lines.append(snippet)
    lines.append("")
    lines.append(
        "Re-emit the complete code in a ```python fenced block that parses "
        "cleanly. Do not truncate; do not leave a dangling bracket, colon, "
        "or unterminated string."
    )
    return "\n".join(lines)
