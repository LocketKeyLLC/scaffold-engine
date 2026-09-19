"""§17.895 — static scan: every in-SPA navigation must target a REGISTERED route.

The bug class this guards, found live on 2026-09-01. §17.859 collapsed a job's
six peer views into the job hub and retired `#/approvals/:id`, `#/output/:id`,
`#/dag/:id` — but three call sites kept pointing at them:

  * ``compose.js`` navigated to ``/approvals/<job_id>`` after every idea
    submission — so submitting an idea, the very first step of the product,
    landed nowhere;
  * ``command_palette.js`` routed ALL THREE of its job-jump statuses to retired
    routes, so no palette job jump worked at all.

It failed SILENTLY: ``router.setNotFound`` renders the Dashboard, so a dead
link looks like "the app went somewhere else" rather than an error. Nothing
tested it, and the operator experienced it as "the movement between the idea,
to approving, to assist" having disappeared.

This is a pure static scan (no imports, no services) so it runs in ci-tier-0
alongside the other inventory gates.
"""
from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "app" / "ui" / "static"
_APP_JS = _STATIC / "app.js"

# `router.route("/job/:jobId/:tab", …)`
_ROUTE_RE = re.compile(r'router\.route\("([^"]+)"')

# Any string/template literal that looks like a path. Template interpolations
# are allowed inside and collapse to a single segment.
_LITERAL_RE = re.compile(r'[`"\'](#?/(?:\$\{[^}]*\}|[^`"\'\s?#])*)')

# Lines that hand a path to the router. Matching the LINE (not the character
# right after the paren) is deliberate: the original bug was written as
# ``router.navigate(jobId ? `/approvals/${jobId}` : "/")`` — a ternary, so an
# anchored "call-paren then quote" matcher sails straight past it. That is
# exactly how this guard first failed to catch the bug it was written for.
#
# ``return`` is included because route helpers hand back bare path literals
# (``command_palette.routeForJob``, ``job_hub.jobHref``) that are navigated
# with LATER — all three of routeForJob's targets were dead and no
# call-site-anchored scan could see them. Server/API paths are passed inline to
# ``api.*`` rather than returned as bare literals, so this stays clean.
# NOTE: `return` is intentionally NOT anchored to the start of the line —
# `if (j.status === "completed") return `/output/${j.id}`;` is the exact shape
# of one of the three dead palette targets.
_NAV_LINE_RE = re.compile(
    r'router\.navigate\(|\bnavigate\(|location\.hash\s*=|\breturn\s+[`"\']#?/'
)


def _segments(path: str) -> int:
    return len([s for s in path.split("/") if s])


def _registered() -> dict[str, set[int]]:
    """head segment -> the set of segment COUNTS the router accepts for it."""
    out: dict[str, set[int]] = {}
    for pattern in _ROUTE_RE.findall(_APP_JS.read_text()):
        n = _segments(pattern)
        head = pattern.split("/")[1] if n else ""
        out.setdefault(head, set()).add(n)
    return out


def test_router_has_routes():
    """Guard the guard: a regex that silently matches nothing proves nothing."""
    reg = _registered()
    assert len(reg) >= 10, f"only found {len(reg)} route heads — parser is broken"
    assert reg.get("job") == {2, 3}, reg.get("job")


def _spa_targets() -> list[tuple[str, int, str]]:
    """(file, line, path) for every literal that navigates within the SPA.

    Two sources, because neither alone is sufficient:
      * any ``#/…`` literal anywhere — the leading ``#`` is unambiguous, so an
        API path like ``/jobs/${id}`` (a SERVER route, correctly not in the
        router) can never be mistaken for one;
      * every path literal on a ``router.navigate`` / ``location.hash`` line,
        where the ``#`` is optional and ternaries are common.
    """
    out: list[tuple[str, int, str]] = []
    for js in sorted(_STATIC.rglob("*.js")):
        for lineno, line in enumerate(js.read_text().splitlines(), 1):
            # Comment lines document route SHAPES ("#/segment/:param") and
            # retired routes by name; neither is a live navigation.
            if line.lstrip().startswith(("//", "*", "/*")):
                continue
            nav_line = bool(_NAV_LINE_RE.search(line))
            for m in _LITERAL_RE.finditer(line):
                raw = m.group(1)
                if raw.startswith("#") or nav_line:
                    out.append((js.name, lineno, raw))
    return out


def test_every_in_spa_link_resolves_to_a_registered_route():
    reg = _registered()
    targets = _spa_targets()
    assert len(targets) > 30, f"only scanned {len(targets)} links — matcher broken"
    dead: list[str] = []
    for name, lineno, raw in targets:
        norm = re.sub(r"\$\{[^}]*\}", "X", raw.lstrip("#")).rstrip("/")
        n = _segments(norm)
        head = norm.split("/")[1] if n else ""
        if head not in reg:
            dead.append(f"{name}:{lineno} -> {raw} (no such route)")
        elif n not in reg[head]:
            dead.append(
                f"{name}:{lineno} -> {raw} "
                f"({n} segments; router accepts {sorted(reg[head])})"
            )
    assert not dead, (
        "in-SPA links pointing at routes the router does not register — these "
        "fall through setNotFound to the Dashboard and fail SILENTLY:\n  "
        + "\n  ".join(dead)
    )


# ── §17.1114 (Phase 1 ledger U-1) — links the ENGINE emits into chat/CLI ─────
#
# The scan above covers app/ui/static only. `pipelines/scaffold_router.py`
# handed the operator `/ui/#/output/{job_id}` in every truncated node-output
# reply — a route retired in §17.859 — and a pipeline test PINNED the dead link.
# Python-emitted `#/…` links are scanned here against the same router registry.
_PY_ROOTS = [
    Path(__file__).resolve().parents[1] / "pipelines",
    Path(__file__).resolve().parents[1] / "app" / "native_chat",
    Path(__file__).resolve().parents[1] / "app" / "modules",
    Path(__file__).resolve().parents[1] / "app" / "routers",
    Path(__file__).resolve().parents[1] / "cli" / "scaffold_cli",
]
_PY_LINK_RE = re.compile(r"#/(?:\{[^}]*\}|[^\s\]\)\"'`<>]*)")


def _python_targets() -> list[tuple[str, int, str]]:
    out: list[tuple[str, int, str]] = []
    for root in _PY_ROOTS:
        if not root.exists():
            continue
        for py in sorted(root.rglob("*.py")):
            for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                if line.lstrip().startswith("#"):
                    continue
                for m in _PY_LINK_RE.finditer(line):
                    out.append((py.name, lineno, m.group(0)))
    return out


def _dead(targets, reg) -> list[str]:
    dead = []
    for name, lineno, raw in targets:
        norm = re.sub(r"\{[^}]*\}|\$\{[^}]*\}", "X", raw.lstrip("#")).rstrip("/")
        n = _segments(norm)
        head = norm.split("/")[1] if n else ""
        if head not in reg:
            dead.append(f"{name}:{lineno} -> {raw} (no such route)")
        elif n not in reg[head]:
            dead.append(f"{name}:{lineno} -> {raw} ({n} segments; router accepts {sorted(reg[head])})")
    return dead


def test_python_link_scanner_hits_the_dead_shape():
    """Guard the guard: the exact string that shipped dead must be flagged."""
    reg = _registered()
    assert _dead([("x.py", 1, "#/output/{job_id}")], reg) == ["x.py:1 -> #/output/{job_id} (no such route)"]
    assert _dead([("x.py", 1, "#/job/{job_id}/output")], reg) == []


def test_every_engine_emitted_link_resolves_to_a_registered_route():
    reg = _registered()
    targets = _python_targets()
    assert targets, "no Python-emitted #/ links found — the scanner went blind"
    dead = _dead(targets, reg)
    assert not dead, (
        "links the ENGINE hands the operator (chat replies, CLI) point at routes the SPA "
        "does not register:\n  " + "\n  ".join(dead)
    )


def test_unknown_routes_render_a_visible_not_found_page():
    """§17.1114 — the silent fallback to the Dashboard is gone."""
    src = _APP_JS.read_text()
    assert 'setNotFound(() => loadAndRender("dashboard"' not in src
    assert 'loadAndRender("notfound"' in src
    assert (_STATIC / "views" / "notfound.js").exists()

