"""§17.164 — regression guard against function-local imports that shadow
module-level bindings.

The §17.164 bug: ``app/main.py:lifespan`` had ``import asyncio`` inside
its body, which made ``asyncio`` local to the entire function scope
(Python's binding rule). The earlier reference to ``asyncio`` at the
Milvus-connect block then raised ``cannot access local variable
'asyncio' where it is not associated with a value``. The surrounding
try/except swallowed the error, the Milvus connect handshake never
completed, and downstream code took the auto-create-empty-collection
path — silently orphaning Milvus data on every restart.

This test scans every .py file under ``app/`` for the same pattern:
a function-local ``import X`` (or ``from M import X``) where ``X`` is
ALSO bound at module level AND referenced in the same function at a
lineno BEFORE the local import. That triple is the active-bug shape.

Function-local imports that are NOT shadows of module-level names, and
shadows where the name is only referenced AFTER the local import line,
are not flagged — both are legal Python and don't trip UnboundLocalError.
"""
from __future__ import annotations

import ast
import pathlib


def _module_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for a in stmt.names:
                names.add(a.asname or a.name.split(".")[0])
        elif isinstance(stmt, ast.ImportFrom):
            for a in stmt.names:
                names.add(a.asname or a.name)
    return names


def _active_shadow_bugs(path: pathlib.Path) -> list[tuple[str, int, int, int, str]]:
    """Return list of (function_name, fn_lineno, ref_lineno, import_lineno, name)."""
    src = path.read_text()
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    module_imports = _module_level_names(tree)
    out: list[tuple[str, int, int, int, str]] = []
    for n in ast.walk(tree):
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        # Find local imports + the names they bind.
        local_imports: list[tuple[int, str]] = []
        for sub in ast.walk(n):
            if isinstance(sub, ast.Import):
                for a in sub.names:
                    name = a.asname or a.name.split(".")[0]
                    if name in module_imports:
                        local_imports.append((sub.lineno, name))
            elif isinstance(sub, ast.ImportFrom):
                for a in sub.names:
                    name = a.asname or a.name
                    if name in module_imports:
                        local_imports.append((sub.lineno, name))
        # For each, find a Name reference with the same id at an earlier lineno.
        for imp_line, name in local_imports:
            for sub in ast.walk(n):
                if (isinstance(sub, ast.Name)
                        and sub.id == name
                        and sub.lineno < imp_line):
                    out.append((n.name, n.lineno, sub.lineno, imp_line, name))
                    break
    return out


def test_no_active_shadow_imports_in_app():
    """No function-local import in app/ may shadow a module-level binding
    that the function ALSO references at a lineno before the local import.

    This is the precise active-bug shape from §17.164 (lifespan +
    import asyncio + earlier asyncio reference). Inactive shadows
    (local-only modules; refs only after the local import) are allowed —
    they're hygiene-bad but don't trigger UnboundLocalError at runtime.
    """
    app_dir = pathlib.Path(__file__).resolve().parents[1] / "app"
    all_hits: list[str] = []
    for p in sorted(app_dir.rglob("*.py")):
        hits = _active_shadow_bugs(p)
        for fn_name, fn_line, ref_line, imp_line, name in hits:
            rel = p.relative_to(app_dir.parent)
            all_hits.append(
                f"{rel}  fn={fn_name}@L{fn_line}  "
                f"ref@L{ref_line}  import@L{imp_line}  name={name}"
            )
    assert not all_hits, (
        "Function-local imports that shadow module-level names AND are "
        "referenced earlier in the same function trigger UnboundLocalError "
        "at runtime (same bug shape as §17.164). Either remove the redundant "
        "local import or rename the local binding:\n  "
        + "\n  ".join(all_hits)
    )
