"""§17.942 — static scan: every declared route must be in `docs/openapi.json`.

The gap this closes, hit on 2026-09-05: adding
`GET /assist/{sid}/steps` and `POST /assist/{sid}/step/goto` (§17.938) drifted
the committed OpenAPI snapshot, and `ci-tier-0` — the pre-push hook — did not
notice. `make openapi-check` catches it, but it runs `docker exec` against a
live orchestrator, so it cannot be a ci-tier-0 prereq without breaking that
tier's contract ("NO docker, NO live services, ~2s"). It is a separate target
nobody runs by reflex, so the drift reached a PR and was only caught because
an operator instruction said to check schema gates by hand.

This is the static half: parse the route decorators out of `app/routers/*.py`
and `app/main.py` with `ast` and compare the path set against the snapshot. It
needs no imports, no services and no container, so it belongs in ci-tier-0
alongside the other inventory scans.

It deliberately does NOT check request/response SHAPES — that still needs the
live app and stays in `make openapi-check`. What it catches is the drift class
that actually bit: a route added or removed without regenerating the snapshot.
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SPEC = _ROOT / "docs" / "openapi.json"
_HTTP_METHODS = {"get", "post", "put", "patch", "delete", "head", "options"}


def _router_prefixes(tree: ast.Module) -> dict[str, str]:
    """Map router variable → its `prefix=` (several modules define more than
    one router, and four of them carry a prefix)."""
    out: dict[str, str] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Assign) and isinstance(node.value, ast.Call)):
            continue
        fn = node.value.func
        if (getattr(fn, "id", None) or getattr(fn, "attr", None)) != "APIRouter":
            continue
        prefix = ""
        for kw in node.value.keywords:
            if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                prefix = str(kw.value.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                out[target.id] = prefix
    return out


def _declared_paths(path: Path) -> set[str]:
    tree = ast.parse(path.read_text())
    prefixes = _router_prefixes(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if not (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)):
                continue
            if dec.func.attr not in _HTTP_METHODS:
                continue
            owner = getattr(dec.func.value, "id", None)
            if owner is None or not dec.args:
                continue
            if not isinstance(dec.args[0], ast.Constant):
                continue  # a computed path (settings.metrics_path) — not static
            # A route that opts out of the schema is correctly absent from the
            # snapshot; flagging it would make the gate cry wolf forever.
            opted_out = any(
                kw.arg == "include_in_schema"
                and isinstance(kw.value, ast.Constant)
                and kw.value.value is False
                for kw in dec.keywords
            )
            if opted_out:
                continue
            if owner in prefixes:
                found.add(prefixes[owner] + str(dec.args[0].value))
            elif owner == "app":
                found.add(str(dec.args[0].value))
    return found


def _all_declared() -> set[str]:
    paths: set[str] = set()
    for f in sorted((_ROOT / "app" / "routers").glob("*.py")):
        paths |= _declared_paths(f)
    paths |= _declared_paths(_ROOT / "app" / "main.py")
    return paths


def _snapshot_paths() -> set[str]:
    return set(json.loads(_SPEC.read_text())["paths"])


def test_the_scan_finds_the_route_surface():
    """A guard that silently matched nothing would pass forever. Pin that the
    scan actually resolves a realistic surface, including a prefixed router."""
    declared = _all_declared()
    assert len(declared) > 100, f"scan resolved only {len(declared)} routes"
    assert "/assist/{session_id}/turns" in declared          # plain router
    assert "/design/spec" in declared or any(
        p.startswith("/design/") for p in declared)          # prefixed router


def test_every_declared_route_is_in_the_openapi_snapshot():
    """The §17.938 drift: two new endpoints, snapshot not regenerated, pre-push
    hook silent."""
    missing = sorted(_all_declared() - _snapshot_paths())
    assert not missing, (
        "These routes are declared in the code but absent from "
        "docs/openapi.json — regenerate it with `make openapi-snapshot` "
        "(and bump the FastAPI version= field if the change is breaking):\n"
        + "\n".join(f"  - {p}" for p in missing)
    )


def test_no_stale_paths_left_in_the_snapshot():
    """The other direction: a route deleted from the code but still advertised
    to SDK consumers."""
    stale = sorted(_snapshot_paths() - _all_declared())
    assert not stale, (
        "These paths are in docs/openapi.json but no longer declared in the "
        "code — regenerate it with `make openapi-snapshot`:\n"
        + "\n".join(f"  - {p}" for p in stale)
    )
