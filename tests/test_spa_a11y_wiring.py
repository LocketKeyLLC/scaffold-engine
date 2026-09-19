"""§17.1118 (Phase 1 ledger U-11) — focus management and labelled controls, as a gate.

The replan modal and the plan drawer had no dialog role, no focus move, no Tab
trap, no Escape, no return-focus; six icon-only controls had hover text as
their only label. `components.openDialog` is the one helper; this keeps every
overlay on it and every icon-only control labelled. Static, ci-tier-0.
"""
from __future__ import annotations

import re
from pathlib import Path

_STATIC = Path(__file__).resolve().parents[1] / "app" / "ui" / "static"
_ICON_ONLY = re.compile(r'text:\s*"(✕|⋯|\?|↻|✓|✎)"')


def _views():
    return sorted((_STATIC / "views").glob("*.js"))


def test_icon_only_controls_carry_an_aria_label():
    offenders = []
    for js in _views():
        lines = js.read_text(encoding="utf-8").splitlines()
        for i, line in enumerate(lines):
            if not _ICON_ONLY.search(line):
                continue
            # only CONTROLS need a label; a decorative glyph in a span does not.
            # The element the glyph belongs to is the NEAREST `el("tag"` before it.
            m = _ICON_ONLY.search(line)
            before = "\n".join(lines[max(0, i - 4): i]) + "\n" + line[:m.start()]
            tags = re.findall(r'el\("(\w+)"', before)
            if not tags or tags[-1] not in ("button", "summary", "a"):
                continue
            window = "\n".join(lines[max(0, i - 4): i + 4])
            if "aria-label" not in window:
                offenders.append(f"{js.name}:{i + 1}: {line.strip()[:80]}")
    assert not offenders, "icon-only controls without an aria-label (hover text is not a label):\n  " + "\n  ".join(offenders)


def test_every_modal_overlay_is_a_real_dialog():
    """A `.modal-overlay` creation must be followed by openDialog(...) in the
    same function — role, focus in, Tab trap, Escape, focus back."""
    for js in _views():
        src = js.read_text(encoding="utf-8")
        for m in re.finditer(r'class: "modal-overlay"', src):
            tail = src[m.end(): m.end() + 4000]
            assert "openDialog(" in tail, f"{js.name}: a modal-overlay is built without openDialog()"


def test_the_plan_drawer_is_a_labelled_dialog_with_escape_and_focus_return():
    plan = (_STATIC / "views" / "plan.js").read_text(encoding="utf-8")
    assert 'role: "dialog", "aria-label": "Step editor"' in plan
    assert "openDialog(drawer" in plan and "drawerDialog.close()" in plan


def test_the_dialog_helper_exists_and_is_complete():
    comp = (_STATIC / "components.js").read_text(encoding="utf-8")
    assert "export function openDialog" in comp
    for token in ('setAttribute("role", "dialog")', '"Escape"', '"Tab"', "opener.focus()", "aria-modal"):
        assert token in comp, f"openDialog lacks {token}"


def test_hub_tabs_are_keyboard_navigable():
    hub = (_STATIC / "views" / "job_hub.js").read_text(encoding="utf-8")
    assert '"ArrowLeft"' in hub and '"ArrowRight"' in hub and 'role: "tablist"' in hub
