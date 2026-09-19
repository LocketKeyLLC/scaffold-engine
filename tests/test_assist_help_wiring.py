"""§17.1096 — the in-product help path is WIRED to the controls, as a gate.

The operator's complaint was that the engine does a great deal on its own
(restart recovery, state verification, plan-change proposals) and the controls
carried hover-only tooltips, but nothing in the web UI explained any of it —
there was no discoverable path. The fix is a "?" Help panel built from ONE
source (`ASSIST_HELP` in assist.js). This gate makes the wiring an invariant,
the same shape as the operator-field inventory (§17.1007c) and the poll/modal
registry (§17.1094): every `verb("X", ...)` in the file MUST have a plain-
language entry in ASSIST_HELP.controls, and the discoverable "?" path must
exist. A new control that ships without its explanation fails here, not in
front of the operator.
"""
from __future__ import annotations

import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
ASSIST = ROOT / "app" / "ui" / "static" / "views" / "assist.js"
SRC = ASSIST.read_text(encoding="utf-8")


def _verb_labels() -> set[str]:
    """Every label passed to verb("LABEL", ...) — the controls the operator sees."""
    return set(re.findall(r'\bverb\(\s*"((?:[^"\\]|\\.)*)"', SRC))


def _control_help_keys() -> set[str]:
    """The keys of ASSIST_HELP.controls — the explained controls."""
    m = re.search(r"controls:\s*\{", SRC)
    assert m, "ASSIST_HELP.controls block not found"
    i = m.end()
    depth, j = 1, i
    while j < len(SRC) and depth:
        if SRC[j] == "{":
            depth += 1
        elif SRC[j] == "}":
            depth -= 1
        j += 1
    block = SRC[i:j]
    # keys are quoted strings immediately before a colon at this nesting level
    return set(re.findall(r'"((?:[^"\\]|\\.)*)"\s*:', block))


def test_every_verb_has_a_help_entry():
    verbs = _verb_labels()
    helped = _control_help_keys()
    assert verbs, "no verb(...) controls found — parser drift"
    missing = verbs - helped
    assert not missing, (
        "control(s) with no plain-language help in ASSIST_HELP.controls — a "
        "button must not ship without an explanation the '?' panel can show:\n  "
        + "\n  ".join(sorted(missing)))


def test_no_orphan_help_entries():
    """A help entry for a control that no longer exists is stale — remove it."""
    orphan = _control_help_keys() - _verb_labels()
    assert not orphan, "ASSIST_HELP.controls describes control(s) that no longer exist: " + ", ".join(sorted(orphan))


def test_the_discoverable_help_path_exists():
    """A visible '?' button, a panel it toggles, and the pure sections source —
    hover tooltips alone are not a path (invisible on touch, undiscoverable)."""
    assert 'text: "? Help"' in SRC, "no visible '? Help' button in the header"
    assert "function toggleHelp(" in SRC and "helpPanel" in SRC, "no toggleable help panel"
    assert "export function helpSections(" in SRC, "helpSections() (the panel's source) is not exported/testable"
    assert "export const ASSIST_HELP" in SRC
    # the panel links out to the Capabilities page (read-only list is not admin-gated)
    assert 'href: "#/capabilities"' in SRC, "help panel does not link to the Capabilities page"


def test_help_covers_the_behaviours_built_this_session():
    """The behaviours that had no UI explanation must each be described: restart
    recovery, the status line, plan/reality reconciliation, plan-change
    proposals, sourced-values-only, and the system map."""
    m = re.search(r"behaviors:\s*\[", SRC)
    assert m, "ASSIST_HELP.behaviors not found"
    block = SRC[m.end():SRC.index("],", m.end())]
    titles = re.findall(r'title:\s*"((?:[^"\\]|\\.)*)"', block)
    joined = " ".join(titles).lower()
    for needle in ("restart", "status line", "plan", "proposal", "invent", "your setup"):
        assert needle in joined, f"no behaviour explains {needle!r}: {titles}"


# ── §17.1117 (Phase 1 ledger U-8) — a behaviour the help panel describes must EXIST ──
#
# The "status line" entry described a "small pulse dot" that no code rendered:
# `pulse()` bumped a timestamp and `assist_turn_pulse` drew nothing. A help
# panel is the discoverable path (§17.1096) only while it describes real
# controls. Each phrase here must map to a DOM class that both the view AND
# the stylesheet know.
# (class, needs_css): the dot and the spinner carry their own look; the clock
# is text inside the styled status line.
_HELP_PHRASE_TO_CLASS = {
    "dot beside it": ("assist-pulse", True),
    "spinner": ("spin", True),
    "clock": ("status-text", False),
}


def test_every_described_status_line_control_exists_in_view_and_css():
    css = (ROOT / "app" / "ui" / "static" / "app.css").read_text(encoding="utf-8")
    m = re.search(r'title: "The status line above the box", plain: "([^"]+)"', SRC)
    assert m, "the status-line help entry is gone"
    plain = m.group(1)
    for phrase, (cls, needs_css) in _HELP_PHRASE_TO_CLASS.items():
        assert phrase in plain, f"help entry no longer mentions {phrase!r} — update the registry"
        assert f'class: "{cls}"' in SRC or f'"{cls}"' in SRC, f"{phrase!r} described but .{cls} not rendered by assist.js"
        if needs_css:
            assert f".{cls}" in css, f".{cls} has no styles"
    assert 'case "assist_turn_pulse":' in SRC and "pulse();" in SRC, "the pulse frame must drive the dot"
    assert "pulseState(" in SRC, "the dot's state must come from pulseState (live/quiet/stale)"

