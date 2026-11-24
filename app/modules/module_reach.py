"""§17.2000 — a module nothing reaches is not done: which of the engine's modules an entry point imports, transitively.

Standard library only: `scripts/deliver_job.py` reads it from the checkout it writes into, without the engine's
dependencies.

Live (69fe61c4 "Paper-Trading Lab Operator Dashboard", 2026-11-22): T9 wrote `paper_serve_static_asset_under` (the
StaticFiles mount the page's Chart.js loads from) and T10 `paper_enforce_read_only_data` (the read-only session "on the
dashboard's query path"); each passed its own tests, nothing imported either, and the delivered page loaded a script
that 404s while the route read through a plain session. The plan had them start FROM the lab's entry step (T8), so the
entry could never import them, and neither was allowed to edit it."""
from __future__ import annotations

import ast
import re
from collections.abc import Iterable

_PKG = "app.modules"
#: `importlib.import_module("app.modules.x")` and the like: a module named in a string is imported too
_DOTTED_RE = re.compile(r"^app\.modules\.([a-z_][a-z0-9_]*)$")


def imported_modules(code: str, package: str | None = _PKG) -> set[str]:
    """The `app.modules` leaves ``code`` imports: `import app.modules.x`, `from app.modules import x`,
    `from app.modules.x import y`, a relative `from . import x` / `from .x import y` (``package``: the code's own
    package; None for a file outside `app/modules`), and a string that is exactly `app.modules.x`. Empty when the code
    does not parse."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return set()
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name.startswith(_PKG + "."):
                    out.add(a.name.split(".")[2])
        elif isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if n.level == 1 and package == _PKG:
                mod = f"{_PKG}.{mod}" if mod else _PKG
            elif n.level:
                continue
            if mod == _PKG:
                out.update(a.name for a in n.names)
            elif mod.startswith(_PKG + "."):
                out.add(mod.split(".")[2])
        elif isinstance(n, ast.Constant) and isinstance(n.value, str) and (m := _DOTTED_RE.match(n.value)):
            out.add(m.group(1))
    return out


def reached(codes: dict[str, str], seeds: Iterable[str]) -> set[str]:
    """Every module in ``codes`` (leaf -> code) that ``seeds`` reach: a seed itself, and whatever a reached module
    imports, transitively. A leaf ``codes`` does not hold is reached but read no further."""
    seen: set[str] = set()
    todo = list(seeds)
    while todo:
        m = todo.pop()
        if m in seen:
            continue
        seen.add(m)
        if m in codes:
            todo.extend(imported_modules(codes[m]) - seen)
    return seen


def entries(codes: dict[str, str], candidates: Iterable[str]) -> list[str]:
    """§17.2004 — of the modules that expose `install(app)` (``candidates``), the lab's own entries: those no OTHER
    candidate reaches through its imports. A helper the entry imports and calls is part of the entry, not a second one.
    Live (69fe61c4 T12, 2026-11-24): `paper_serve_static_asset_under` mounts the static files through its own
    `install(app)`, and the lab's entry `paper_wire_engine_entry_module` imports and calls it (§17.2000 REACH); the
    delivery check asked for both in `app/labs.py` and refused the operator's "merged and redeployed". Two candidates
    that reach each other are both kept."""
    cands = list(dict.fromkeys(candidates))
    reach = {c: reached(codes, [c]) - {c} for c in cands}
    return [c for c in cands if not any(c in reach[o] and o not in reach[c] for o in cands if o != c)]
