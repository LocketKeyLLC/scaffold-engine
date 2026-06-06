"""§17.428 — structural checkers for codegen goldens.

Deterministic, no-LLM assertion machinery shared by the offline golden
tests (``test_codegen_golden.py``). Intentionally NOT exact-match: LLM
output is non-deterministic, so goldens assert *structural facts* about
the code, mirroring the substring philosophy of ``test_retrieval_golden``.

When the live end-to-end golden tier lands (deferred from §17.428), it
reuses these same checkers against real model output.
"""
from __future__ import annotations

import ast

from app.modules.execution_codegen_gate import (
    PYTHON_LANGS,
    check_python_syntax,
    extract_code_blocks,
)


def _python_code(output: str) -> str:
    """Concatenate all Python fenced blocks in output (newline-joined)."""
    return "\n\n".join(
        code for lang, code in extract_code_blocks(output) if lang in PYTHON_LANGS
    )


def defines_symbol(output: str, name: str) -> bool:
    """True if any Python block defines ``name`` as a top-level function,
    class, async function, or module-level assignment target."""
    code = _python_code(output)
    if not code:
        return False
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name == name:
                return True
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == name:
                    return True
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and node.target.id == name:
                return True
    return False


def check_golden(golden: dict, output: str) -> list[str]:
    """Run a golden's structural assertions against ``output``.

    Returns a list of human-readable failure strings; empty list = pass.

    Supported assertion keys (all optional):
        must_parse: bool        — every Python block parses (ast.parse)
        must_define: [str]      — each name is defined at module level
        must_not_contain: [str] — none of these substrings appear in the code
        must_contain: [str]     — all of these substrings appear in the code
    """
    failures: list[str] = []
    code = _python_code(output)

    if golden.get("must_parse"):
        if not code:
            failures.append("must_parse: no Python fenced block found")
        else:
            findings = check_python_syntax(output)
            if findings:
                failures.append(
                    "must_parse: SyntaxError(s): "
                    + "; ".join(f"block {f.block_index} line {f.lineno}: {f.message}" for f in findings)
                )

    for name in golden.get("must_define", []):
        if not defines_symbol(output, name):
            failures.append(f"must_define: symbol '{name}' not defined at module level")

    for substr in golden.get("must_not_contain", []):
        if substr in code:
            failures.append(f"must_not_contain: banned construct '{substr}' present")

    for substr in golden.get("must_contain", []):
        if substr not in code:
            failures.append(f"must_contain: expected '{substr}' missing")

    return failures
