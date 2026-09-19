"""§17.1094 — the modal-and-poll wiring audit, as a gate.

A background refresh must never fight the operator: a poll (setInterval) or an
SSE-driven render may not force-open a modal, and may not re-mount a view over
input the operator is typing. §17.1093 was one instance — the idle poll
re-opened the replan modal every 25 s. This gate makes the whole CLASS a test
failure instead of a live incident, the same shape as the operator-field
inventory (§17.1007c): every timer is enumerated from the code and must be
registered here with the guard that makes it safe; an unregistered or
unguarded one fails.

Registry columns:
- key: (file basename, a substring identifying the callback) — unique per timer.
- guard: the token in the file that makes the timer safe. One of:
    "document.hidden"  — skips a backgrounded tab;
    "disposed"         — stops after the view is torn down;
    "stopWaitPoll"     — the poll is stopped before any input renders;
    "background"       — the render it triggers is tagged background (never
                         force-opens a modal; §17.1093);
    "no-remount"       — the callback updates a text/badge node in place and
                         never re-mounts an editable region or a modal.
- renders: does the callback trigger a re-render / mount of view content?
- opens_modal: could the callback path open a modal? (must then be "background").
"""
from __future__ import annotations

import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPA = ROOT / "app" / "ui" / "static"

# The audited timers. Adding a setInterval to the SPA without adding it here
# fails test_every_timer_is_registered — which is the point.
POLL_REGISTRY: dict[tuple[str, str], dict] = {
    ("dashboard.js", "setInterval(() => { if (!document.hidden) load(); }, 10000)"): {"guard": "document.hidden", "renders": True, "opens_modal": False},
    ("approvals.js", "setInterval(() => { if (!document.hidden) load(); }, 10000)"): {"guard": "document.hidden", "renders": True, "opens_modal": False},
    ("approvals.js", "setInterval(() => { if (!document.hidden) load(); }, 4000)"): {"guard": "stopWaitPoll", "renders": True, "opens_modal": False},
    ("approvals.js", "setInterval(pollStatus, 2500)"): {"guard": "no-remount", "renders": False, "opens_modal": False},
    ("assist.js", "setInterval(paintStatus, 1000)"): {"guard": "no-remount", "renders": False, "opens_modal": False},
    ("assist.js", "idlePoll = setInterval"): {"guard": "announce-once", "renders": True, "opens_modal": True},
    ("app.js", "attentionTimer = setInterval"): {"guard": "no-remount", "renders": False, "opens_modal": False},
    ("app.js", "healthTimer = setInterval"): {"guard": "document.hidden", "renders": True, "opens_modal": False},
}

_SPA_JS = sorted(list(SPA.glob("*.js")) + list((SPA / "views").glob("*.js")))
_SETINTERVAL_RE = re.compile(r"setInterval\s*\(")


def _timers() -> list[tuple[str, int, str, str]]:
    """(basename, lineno, the setInterval line, full file text) per timer."""
    out = []
    for path in _SPA_JS:
        src = path.read_text(encoding="utf-8")
        for m in _SETINTERVAL_RE.finditer(src):
            line_start = src.rfind("\n", 0, m.start()) + 1
            line_end = src.find("\n", m.start())
            out.append((path.name, src.count("\n", 0, m.start()) + 1, src[line_start:line_end].strip(), src))
    return out


def _match_registry(basename: str, line: str) -> tuple[tuple[str, str], dict] | None:
    for (f, needle), spec in POLL_REGISTRY.items():
        if f == basename and needle in line:
            return (f, needle), spec
    return None


def test_every_timer_is_registered():
    """A new setInterval must be added to POLL_REGISTRY with its guard — the
    author has to state why a background timer is safe."""
    unregistered = [f"{b}:{ln} -> {line}" for b, ln, line, _ in _timers() if _match_registry(b, line) is None]
    assert not unregistered, (
        "unregistered SPA timer(s) — declare each in POLL_REGISTRY with the guard "
        "that keeps a background refresh from fighting the operator:\n  " + "\n  ".join(unregistered))


def test_every_registered_guard_token_is_present_in_its_file():
    """The guard a timer claims must actually be in the file (not a stale note)."""
    _TOKEN = {
        "document.hidden": "document.hidden",
        "disposed": "disposed",
        "stopWaitPoll": "stopWaitPoll(",
        "background": "background: true",
        "announce-once": "announcedReplanSigs",  # §17.1097 — opens at most once per proposal
        "no-remount": None,   # asserted structurally by the two tests below
    }
    missing = []
    for (f, needle), spec in POLL_REGISTRY.items():
        tok = _TOKEN[spec["guard"]]
        if tok is None:
            continue
        src = (SPA / ("views/" + f) if (SPA / "views" / f).exists() else SPA / f).read_text(encoding="utf-8")
        if tok not in src:
            missing.append(f"{f}: guard '{spec['guard']}' claims token {tok!r} but it is absent")
    assert not missing, "\n".join(missing)


def test_a_modal_opening_poll_is_announce_once_guarded():
    """A timer whose render path can open a modal must open it AT MOST ONCE per
    proposal — the §17.1097 announce-once rule — never on every poll tick. The
    decision opens only on first sighting (sig not yet in `announced`), and the
    renderer records the sig before opening, so a later tick can't re-pop it.
    (Replaces the §17.1093 `background` guard, which suppressed opening entirely
    and made the surfacing inconsistent.)"""
    src = (SPA / "views" / "assist.js").read_text(encoding="utf-8")
    for (f, needle), spec in POLL_REGISTRY.items():
        if not spec["opens_modal"]:
            continue
        assert spec["guard"] == "announce-once", f"{f} {needle}: a modal-opening poll must use the announce-once guard"
    assert "firstSighting = !inSet(announced)" in src, "the decision no longer opens only on first sighting"
    assert "openModal = firstSighting" in src, "openModal is not gated on firstSighting"
    assert "announcedReplanSigs.add(sig)" in src, "the renderer does not record the sig, so a poll tick could re-open it"


def _callback_target(line: str) -> str | None:
    """The function a `setInterval(TARGET, ms)` calls, when it is a bare name."""
    m = re.search(r"setInterval\(\s*([A-Za-z_$][\w$]*)\s*,", line)
    return m.group(1) if m else None


def _function_body(src: str, name: str) -> str:
    """The body of `function NAME(...)` / `async function NAME(...)`, by brace
    match. "" when not found (an inline/arrow callback)."""
    m = re.search(rf"\b(?:async\s+)?function\s+{re.escape(name)}\s*\(", src)
    if not m:
        return ""
    i = src.index("{", m.end())
    depth, j = 0, i
    while j < len(src):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
        j += 1
    return src[i:]


def test_no_poll_remounts_a_view_that_holds_live_input_without_a_stop_guard():
    """A timer that re-renders must not clobber input the operator is typing.
    Scoped to the poll's OWN render function (not the whole file): the approvals
    LIST poll renders a picker with no inputs; the DETAIL poll stops
    (stopWaitPoll) before any answer input renders."""
    offenders = []
    for (f, needle), spec in POLL_REGISTRY.items():
        if not spec["renders"] or spec["guard"] in ("stopWaitPoll", "document.hidden", "background", "announce-once"):
            continue
        path = SPA / "views" / f if (SPA / "views" / f).exists() else SPA / f
        src = path.read_text(encoding="utf-8")
        line = next((ln for ln in src.splitlines() if needle in ln), needle)
        target = _callback_target(line)
        scope = _function_body(src, target) if target else src
        if re.search(r'el\(\s*"(?:input|textarea)"', scope):
            offenders.append(f"{f} {needle}: its render fn {target!r} builds live input under guard {spec['guard']!r}")
    assert not offenders, "\n".join(offenders)


def test_the_replan_render_decision_is_the_only_modal_opener():
    """Every modal open in the SPA goes through renderReplanProposal, whose
    open/show is decided by the pure, node-tested replanRenderDecision
    (§17.1093). A new openModal-style path must come with the same discipline."""
    assist = (SPA / "views" / "assist.js").read_text(encoding="utf-8")
    assert "function replanRenderDecision(" in assist and "shouldOpen = d.openModal" in assist
    # any other view that grows a `.showModal()` / `openModal(` must register a poll guard first
    for path in _SPA_JS:
        if path.name == "assist.js":
            continue
        src = path.read_text(encoding="utf-8")
        assert ".showModal(" not in src, f"{path.name} opens a native <dialog> — route it through a background-aware decision like replanRenderDecision"
