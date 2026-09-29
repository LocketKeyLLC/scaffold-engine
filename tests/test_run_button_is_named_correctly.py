"""§17.1206 — the page must name the button it is telling the operator to press.

*"it states 'Nothing running-press to begin' BUT THE BUTTON IS ABSOLUTELY NO
WHERE ON THE PAGE AT ALL."*

They were right and the page was wrong. The run button's label is mode-aware —
`▶ Run all`, or `✦ Start assist` when assist mode is on — but the stage panel
said "Nothing running — press ▶ to begin." as a fixed string. With assist mode
on, which is how this operator runs, the only button on the surface read
"✦ Start assist" and the instruction pointed at a ▶ that did not exist.

§17.854 had already fixed the BUTTON to follow a live mode change and never
touched the sentence about it — the two-call-site drift again, one of them prose.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

VIEW = Path(__file__).resolve().parents[1] / "app/ui/static/views/theater.js"


@pytest.fixture(scope="module")
def src() -> str:
    return VIEW.read_text()


def test_the_label_has_one_source(src):
    assert "const runLabel = () => (isAssist() ? \"✦ Start assist\" : \"▶ Run all\");" in src
    # and the button uses it rather than repeating the ternary
    assert 'text: runLabel(), onClick: () => toggleRun()' in src


def test_no_prose_hardcodes_the_play_symbol(src):
    """The regression in one line: any sentence that spells ▶ itself is a
    sentence that lies whenever the mode is assist."""
    bad = [ln.strip() for ln in src.splitlines()
           if "▶" in ln and "runLabel" not in ln and not ln.strip().startswith("//")
           and "node_start:" not in ln]
    # the remaining ▶ uses are button labels set directly on runBtn, which IS the button
    assert all("runBtn.textContent" in ln for ln in bad), bad


def test_the_idle_stage_text_is_built_from_the_label(src):
    assert "const idleStageText = () => `Nothing running — press ${runLabel()} to begin.`;" in src
    assert 'text: idleStageText()' in src
    assert "press ▶ to begin" not in src


def test_a_live_mode_change_updates_the_sentence_too(src):
    """§17.854 kept the button honest on a mode toggle. The sentence has to
    follow, or it goes stale the moment the operator switches mode with the
    page open."""
    blk = src[src.index("onExecModeChange(() => {"):src.index("const statusPill")]
    assert "runBtn.textContent = runLabel();" in blk, blk
    assert "stageTitle.textContent = idleStageText();" in blk, blk


def test_the_restart_message_names_the_button_too(src):
    blk = src[src.index("marked running but nothing is executing"):]
    assert "Press ${runLabel()} to carry on" in blk[:400], blk[:400]
