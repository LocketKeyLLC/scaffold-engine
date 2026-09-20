"""§17.1134 (ledger D-6) — ONE next_actions vocabulary across surfaces.

The noise filter (`wait`) was copied inline three times (scaffold_router ×2,
CLI) and the SPA rendered 0 of the registry's 17 actions. Now: the SDK module
is canonical, the pipeline uses the vendored copy, the CLI imports the SDK,
and the SPA's `next_actions.js` mirrors the noise list and labels EVERY action.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SDK = ROOT / "sdk" / "scaffold_client" / "next_actions.py"
SPA = ROOT / "app" / "ui" / "static" / "next_actions.js"
REGISTRY = ROOT / "app" / "modules" / "recovery.py"


def _sdk_noise() -> set[str]:
    tree = ast.parse(SDK.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "_NOISE_ACTIONS" for t in node.targets):
            return set(ast.literal_eval(ast.unparse(node.value).replace("frozenset(", "set(").replace("set(", "").rstrip(")")))
    pytest.fail("_NOISE_ACTIONS not found in the SDK module")


def _spa_noise() -> set[str]:
    m = re.search(r"export const NOISE_ACTIONS = \[([^\]]*)\]", SPA.read_text(encoding="utf-8"))
    assert m, "NOISE_ACTIONS not found in next_actions.js"
    return set(re.findall(r'"([a-z_]+)"', m.group(1)))


def _registry_actions() -> set[str]:
    return set(re.findall(r'"action":\s*"([a-z_]+)"', REGISTRY.read_text(encoding="utf-8")))


def _spa_labels() -> set[str]:
    m = re.search(r"export const ACTION_LABELS = \{(.*?)\n\};", SPA.read_text(encoding="utf-8"), re.S)
    assert m
    return set(re.findall(r"^\s*([a-z_]+):", m.group(1), re.M))


def test_spa_noise_list_mirrors_the_sdk():
    assert _spa_noise() == _sdk_noise()


def test_every_registry_action_has_a_spa_label():
    missing = sorted(_registry_actions() - _spa_labels())
    assert not missing, f"add labels in app/ui/static/next_actions.js ACTION_LABELS: {missing}"


@pytest.mark.parametrize("path", [
    "pipelines/scaffold_router.py", "pipelines/_vendor/_assist_handlers.py",
    "cli/scaffold_cli/main.py", "app/ui/static/components.js", "app/ui/static/views/dashboard.js",
])
def test_no_inline_copy_of_the_noise_filter(path: str):
    p = ROOT / path
    if not p.is_file():
        pytest.skip(f"{path} not in this image")
    src = p.read_text(encoding="utf-8")
    assert not re.search(r"""\.get\(\s*["']action["']\s*\)\s*[!=]=\s*["']wait["']""", src), f"{path}: inline `wait` filter — use filter_renderable / filterRenderable"
    assert not re.search(r"""\.action\s*===?\s*["']wait["']""", src), f"{path}: inline `wait` filter"


def test_the_spa_renders_next_actions_somewhere():
    views = (ROOT / "app" / "ui" / "static" / "views")
    users = [p.name for p in views.glob("*.js") if "nextActionChips(" in p.read_text(encoding="utf-8")]
    assert {"dashboard.js", "theater.js"} <= set(users), users
