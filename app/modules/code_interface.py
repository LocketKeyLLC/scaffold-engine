"""§17.1758 — when upstream code must shrink, a code step is given its interface, not its middle cut out.

Live (job 2772acae T22 "Write integration tests", 2026-10-23): its fourteen upstream modules came to ~147,000 chars and
the upstream block was cut to fit -- each module kept its head and tail with "[...truncated 17,636 chars...]" between.
So the test writer never saw `BacktestResult`'s fields or what `approval_step` imports, wrote
`result.orders`, `BacktestResult(strategy_name, model_name, …)` and `patch("app.modules.approval_step.pending_decision")`,
and failed all three attempts on names that do not exist. What a step that imports, calls or tests a module needs is
exactly what the cut threw away: every name and its signature. This renders that, from the code itself (ast): imports,
module constants, functions with their signatures and first docstring line, classes with their decorators, fields and
method signatures. Bodies are dropped. A text with no parseable Python block yields None, and the caller cuts as before.
"""
from __future__ import annotations

import ast

from app.modules.engine_contracts import query_return, returned_keys
from app.modules.execution_codegen_gate import PYTHON_LANGS, extract_code_blocks


def _doc(node: ast.AST) -> str:
    try:
        d = ast.get_docstring(node)  # type: ignore[arg-type]
    except TypeError:
        return ""
    first = (d or "").strip().splitlines()
    return first[0][:120] if first else ""


def _fn(fn: ast.FunctionDef | ast.AsyncFunctionDef, indent: str, code: str = "") -> list[str]:
    out = [f"{indent}@{ast.unparse(d)}" for d in fn.decorator_list]
    ret = f" -> {ast.unparse(fn.returns)}" if fn.returns else ""
    head = f"{indent}{'async ' if isinstance(fn, ast.AsyncFunctionDef) else ''}def {fn.name}({ast.unparse(fn.args)}){ret}: ..."
    doc = _doc(fn)
    # §17.1834 — and the keys the dicts it returns carry: a signature says `dict[str, Any]`, a first docstring line
    # can say "the equity" of a dict that has no `equity`
    keys = returned_keys(code, fn.name) if code and not indent else []
    query = query_return(code, fn.name) if code and not indent and not keys else ""          # §17.1837
    notes = ([f"returns keys: {', '.join(keys)}"] if keys else []) + ([f"returns {query}"] if query else []) + (
        [doc] if doc else [])
    out.append(head + (f"  # {' -- '.join(notes)}" if notes else ""))
    return out


def _value(node: ast.AST | None, limit: int = 100) -> str:
    if node is None:
        return ""
    s = ast.unparse(node)
    return s if len(s) <= limit else s[:limit] + " …"


def _class(cls: ast.ClassDef) -> list[str]:
    out = [f"@{ast.unparse(d)}" for d in cls.decorator_list]
    bases = ", ".join(ast.unparse(b) for b in cls.bases + cls.keywords) if (cls.bases or cls.keywords) else ""
    doc = _doc(cls)
    out.append(f"class {cls.name}{'(' + bases + ')' if bases else ''}:" + (f"  # {doc}" if doc else ""))
    body = 0
    for n in cls.body:
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            out.append(f"    {n.target.id}: {ast.unparse(n.annotation)}" + (f" = {_value(n.value)}" if n.value else ""))
            body += 1
        elif isinstance(n, ast.Assign):
            out.append(f"    {', '.join(ast.unparse(t) for t in n.targets)} = {_value(n.value)}")
            body += 1
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.extend(_fn(n, "    "))
            body += 1
    if not body:
        out.append("    ...")
    return out


def python_interface(text: str) -> str | None:
    """The interface of every Python block in ``text``; None when there is no parseable Python."""
    rendered: list[str] = []
    for lang, code in extract_code_blocks(text or ""):
        if lang not in PYTHON_LANGS:
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        lines: list[str] = []
        doc = _doc(tree)
        if doc:
            lines.append(f'"""{doc}"""')
        for n in tree.body:
            if isinstance(n, (ast.Import, ast.ImportFrom)):
                lines.append(ast.unparse(n))
            elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                lines.extend(_fn(n, "", code))
            elif isinstance(n, ast.ClassDef):
                lines.extend(_class(n))
            elif isinstance(n, ast.Assign):
                lines.append(f"{', '.join(ast.unparse(t) for t in n.targets)} = {_value(n.value)}")
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                lines.append(f"{n.target.id}: {ast.unparse(n.annotation)}" + (f" = {_value(n.value)}" if n.value else ""))
        if lines:
            rendered.append("```python\n" + "\n".join(lines) + "\n```")
    return "\n\n".join(rendered) if rendered else None


def shrink_code(text: str, share: int, truncate) -> tuple[str, bool]:
    """Fit ``text`` into ``share`` chars: its interface when it has Python (cut further only if even that is too
    long), else ``truncate(text, share)``. Returns (text, used_interface)."""
    iface = python_interface(text)
    if iface is None:
        return truncate(text, share), False
    omitted = max(0, len(text) - len(iface))
    note = f"[interface only, names and signatures exact; {omitted} chars of bodies omitted (§17.1758)]\n"
    body = note + iface
    return (body if len(body) <= share else truncate(body, share)), True
