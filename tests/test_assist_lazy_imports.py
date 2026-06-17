"""§17.540 — guard against stale lazy imports in app/routers/assist.py.

The streaming guide (`/assist/{sid}/guide/stream`) and handoff endpoints use
in-function `from <module> import <name>` (deferred to dodge an import cycle
with app.main). `_sse_with_disconnect_watch` was relocated app.main →
app.utils.sse, but assist.py kept importing it from app.main — so those two
endpoints raised `ImportError` and 500'd at *request* time. Unit tests never
caught it because they don't drive the real import; it only surfaced once
§17.539 history-routing first routed a live user into the streaming guide.

This test statically resolves every `from app.* import ...` in assist.py —
module-top AND in-function — so a moved/renamed symbol fails here instead of
in production.
"""
import ast
import importlib
from pathlib import Path

import pytest


@pytest.mark.smoke
def test_all_assist_app_imports_resolve():
    src = Path(__file__).resolve().parent.parent / "app" / "routers" / "assist.py"
    tree = ast.parse(src.read_text(), filename=str(src))

    unresolved = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        # Only check first-party app.* imports (the ones prone to internal moves).
        if not node.module or not node.module.startswith("app."):
            continue
        try:
            mod = importlib.import_module(node.module)
        except Exception as exc:  # noqa: BLE001
            unresolved.append(f"{node.module} (line {node.lineno}): import failed — {exc}")
            continue
        for alias in node.names:
            if alias.name == "*" or hasattr(mod, alias.name):
                continue
            # Not an attribute — it may be a submodule (`from app.modules import
            # assist_agent`). Resolve that before declaring it broken.
            try:
                importlib.import_module(f"{node.module}.{alias.name}")
            except Exception:  # noqa: BLE001
                unresolved.append(
                    f"{node.module}.{alias.name} (assist.py line {node.lineno})"
                )

    assert not unresolved, (
        "stale/unresolved imports in app/routers/assist.py — a symbol was "
        "moved or renamed:\n  " + "\n  ".join(unresolved)
    )
