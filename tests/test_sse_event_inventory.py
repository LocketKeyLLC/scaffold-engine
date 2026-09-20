"""§17.190 / §17.1133 (ledger D-5) — the SSE event inventory gate.

``app/sse_events.py`` is the single vocabulary. Before §17.1133 this gate
scanned four emitter files for ``_sse("literal")`` and one consumer
(``scaffold_router``) for ``event_type == "literal"``; everything else —
``assist_turn.py`` (``_ev``), ``routers/workflow.py`` (``_sse_event``), the
research modes, the SPA, the CLI, and every constant-based emitter or consumer
— was outside it. Found on the first full scan: a constant defined but missing
from the set (``assist_turn_started``), a CLI loop waiting on ``node_completed``
(dead since §17.190), and three research-mode telemetry events with no
renderer.

Contract (all surfaces, literal or constant):
  1. the vendored OWUI copy is byte-equal (``make check-sse-events`` backstop);
  2. every EMITTED name is in the inventory;
  3. every CONSUMED name (pipeline, vendored handlers, SPA, CLI) is in the
     inventory — a consumer matching a name nobody emits is a dead branch;
  4. every inventory name is emitted somewhere, or declared in
     ``NOT_EMITTED_BY_THE_ORCHESTRATOR`` with the reason;
  5. every emitted name is rendered by SOME consumer, or declared in
     ``UNRENDERED_BY_DESIGN`` with the reason — a new event cannot appear
     without either a renderer or a written decision;
  6. no SPA event switch drops an unknown event silently.
"""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "app" / "sse_events.py"
VENDORED = REPO_ROOT / "pipelines" / "_vendor" / "_sse_events.py"

_spec = importlib.util.spec_from_file_location("_sse_events_src", SOURCE)
_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_mod)  # type: ignore[union-attr]
ALL_EVENT_NAMES: frozenset[str] = _mod.ALL_EVENT_NAMES
CONSTANTS: dict[str, str] = {k: v for k, v in vars(_mod).items() if k.isupper() and isinstance(v, str)}

#: Inventory names the orchestrator never emits itself.
NOT_EMITTED_BY_THE_ORCHESTRATOR: dict[str, str] = {
    "stream_stalled": "synthesized by the OWUI pipeline when a stream goes quiet (§17.360)",
    "blocked": "legacy: the terminal blocked frame is a `done`/status payload since §17.295; the pipeline branch stays for old streams",
}
#: Emitted names no consumer renders by name — each needs a written reason.
UNRENDERED_BY_DESIGN: dict[str, str] = {
    "advance_phase": "POST /jobs/{id}/approve stream telemetry; the SPA follows the detached chain via GET /approve (§17.1036), SDK/CLI readers get every frame",
    "advance_complete": "same stream as advance_phase",
    "stage_start": "design pipeline (/design/{id}/advance) stream — SDK-consumed generically; no SPA sim view",
    "stage_done": "same as stage_start",
    "stage_error": "same as stage_start",
}

_EMIT_CALL = r"\b(?:_sse|_sse_event|_ev|sse_event|emit_sse)\s*\(\s*"
_EMIT_LITERAL = re.compile(_EMIT_CALL + r"""["']([a-z_]+)["']""")
_EMIT_CONST = re.compile(_EMIT_CALL + r"(?:[A-Za-z_]+\.)?([A-Z][A-Z_]+)\b")


def _strip_docstrings(src: str) -> str:
    """Prose that quotes the idiom (``_sse("name", ...)`` in a docstring) is not an emitter."""
    return re.sub(r'"""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\'', "", src)


def _emitted() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for p in sorted((REPO_ROOT / "app").rglob("*.py")):
        if p == SOURCE:
            continue
        src = _strip_docstrings(p.read_text(encoding="utf-8"))
        for name in _EMIT_LITERAL.findall(src):
            out.setdefault(name, set()).add(str(p.relative_to(REPO_ROOT)))
        for const in _EMIT_CONST.findall(src):
            if const in CONSTANTS:
                out.setdefault(CONSTANTS[const], set()).add(str(p.relative_to(REPO_ROOT)))
    return out


_PY_CMP = re.compile(r"""\b(?:event_type|event|ev|evt|etype|event_name|name)\s*(?:==|!=)\s*(?:["']([a-z_]+)["']|(?:[A-Za-z_]+\.)?([A-Z][A-Z_]+))""")
_PY_IN = re.compile(r"""\b(?:event_type|event|ev|etype|name)\s+(?:not\s+)?in\s*\(([^)]*)\)""")
_JS_CASE = re.compile(r"""case\s+["']([a-z_]+)["']\s*:""")


def _py_consumed(path: Path) -> set[str]:
    src = path.read_text(encoding="utf-8")
    names: set[str] = set()
    for lit, const in _PY_CMP.findall(src):
        if lit:
            names.add(lit)
        elif const in CONSTANTS:
            names.add(CONSTANTS[const])
    for grp in _PY_IN.findall(src):
        for tok in grp.split(","):
            tok = tok.strip()
            if re.fullmatch(r"""["'][a-z_]+["']""", tok):
                names.add(tok.strip("\"'"))
            elif tok.split(".")[-1] in CONSTANTS:
                names.add(CONSTANTS[tok.split(".")[-1]])
    return names


def _js_event_switch_cases(path: Path) -> set[str]:
    """`case "x":` labels inside a `switch (event)` block only."""
    src = path.read_text(encoding="utf-8")
    names: set[str] = set()
    for m in re.finditer(r"switch\s*\(\s*(?:event|ev|type|evt)\s*\)\s*\{", src):
        depth, i = 0, m.end() - 1
        while i < len(src):
            if src[i] == "{":
                depth += 1
            elif src[i] == "}":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        names |= set(_JS_CASE.findall(src[m.start():i]))
    return names


CONSUMER_PY = [
    REPO_ROOT / "pipelines" / "scaffold_router.py",
    REPO_ROOT / "pipelines" / "_vendor" / "_assist_handlers.py",
    REPO_ROOT / "cli" / "scaffold_cli" / "main.py",
]
CONSUMER_JS = sorted((REPO_ROOT / "app" / "ui" / "static" / "views").glob("*.js")) + [REPO_ROOT / "app" / "ui" / "static" / "api.js"]


def _consumed() -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for p in CONSUMER_PY:
        if not p.is_file():
            continue  # pipelines/ and cli/ are not in every image (§17.207)
        for n in _py_consumed(p):
            out.setdefault(n, set()).add(p.name)
    for p in CONSUMER_JS:
        for n in _js_event_switch_cases(p):
            out.setdefault(n, set()).add("spa:" + p.name)
    return out


#: Generic frame names every stream may carry; consumers handle them by shape.
GENERIC = {"done", "error", "warning", "heartbeat", "progress", "queued", "cancelled", "message"}


# ── 1. vendor byte-equal ────────────────────────────────────────────────────

def test_sse_events_byte_equal():
    if not VENDORED.is_file():
        pytest.skip("vendored copy not present in this image; `make check-sse-events` covers drift")
    assert SOURCE.read_bytes() == VENDORED.read_bytes(), "run `make sync-sse-events`"


def test_inventory_is_non_empty():
    assert len(ALL_EVENT_NAMES) >= 30


# ── 2. emitted ⊆ inventory ──────────────────────────────────────────────────

def test_the_scan_sees_every_emitter_idiom():
    emitted = _emitted()
    assert len(emitted) >= 45, sorted(emitted)
    for must in ("assist_turn_routed", "advance_phase", "stage_start", "cache_hit_upstream", "assist_guide_delta"):
        assert must in emitted, f"{must} not seen — an emitter idiom fell out of the scan"


def test_emitted_event_names_are_in_inventory():
    unknown = {n: sorted(f) for n, f in _emitted().items() if n not in ALL_EVENT_NAMES}
    assert unknown == {}, f"emitted but not in app/sse_events.py: {unknown}"


# ── 3. consumed ⊆ inventory (a consumer of a name nobody emits is dead code) ─

def test_consumed_event_names_are_in_inventory():
    unknown = {n: sorted(s) for n, s in _consumed().items() if n not in ALL_EVENT_NAMES}
    assert unknown == {}, f"consumer matches a name that is not in the inventory (dead branch?): {unknown}"


# ── 4. inventory ⊆ emitted ∪ declared ───────────────────────────────────────

def test_every_inventory_name_is_emitted_or_declared():
    emitted = set(_emitted())
    dead = sorted(ALL_EVENT_NAMES - emitted - set(NOT_EMITTED_BY_THE_ORCHESTRATOR))
    assert dead == [], f"in the inventory but never emitted — remove it or declare it with a reason: {dead}"
    stale = sorted(set(NOT_EMITTED_BY_THE_ORCHESTRATOR) & emitted)
    assert stale == [], f"declared not-emitted but an emitter exists now — drop the declaration: {stale}"


# ── 5. emitted ⊆ rendered ∪ declared ────────────────────────────────────────

def test_every_emitted_event_is_rendered_or_declared():
    if not (REPO_ROOT / "pipelines" / "scaffold_router.py").is_file():
        pytest.skip("consumer surfaces not present in this image")
    emitted = set(_emitted()) - GENERIC
    rendered = set(_consumed())
    unrendered = sorted(emitted - rendered - set(UNRENDERED_BY_DESIGN))
    assert unrendered == [], (
        "emitted but no consumer (pipeline / vendored handlers / SPA switch / CLI) renders it by name — "
        "add a renderer or declare it in UNRENDERED_BY_DESIGN with the reason: " + str(unrendered)
    )
    stale = sorted(set(UNRENDERED_BY_DESIGN) & rendered)
    assert stale == [], f"declared unrendered but a consumer renders it now — drop the declaration: {stale}"


# ── 6. no silent drop in the SPA ────────────────────────────────────────────

@pytest.mark.parametrize("path", [p for p in CONSUMER_JS if "switch (event)" in p.read_text(encoding="utf-8")],
                         ids=lambda p: p.name)
def test_spa_event_switch_default_is_not_a_bare_break(path: Path):
    src = path.read_text(encoding="utf-8")
    for m in re.finditer(r"switch\s*\(\s*event\s*\)\s*\{", src):
        block = src[m.start():]
        d = re.search(r"default:\s*\n((?:\s*//[^\n]*\n)*)\s*break;", block)
        assert d is None or "console." in d.group(0) or "log(" in d.group(0), (
            f"{path.name}: `switch (event)` default is a bare break — an unknown event vanishes; log it"
        )
