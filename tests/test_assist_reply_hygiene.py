"""§17.908 — what reaches the operator, as opposed to what the model thought.

The live defect (session 613dd1df, T23, turns 1399 and 1404): the reply opened
with the model narrating the operator in the third person — including telling
them what they were feeling — before getting to the answer. The operator read
all of it.
"""
from __future__ import annotations

import pytest

from app.modules.assist_directives import strip_operator_meta_preamble as strip

pytestmark = pytest.mark.asyncio


LIVE_1399 = (
    'The operator\'s "anything" is a non-sequitur—they are likely expressing '
    "frustration or boredom while the VM is hanging, or they are asking for "
    '"anything" that will actually make the installation work.\n\n'
    "Given the **Running Recap**, the current goal is not to install the game yet."
)
LIVE_1404 = (
    'The operator\'s messages "I want to build something" and "anything" are a '
    "pivot away from the current technical struggle with VM 106. They are "
    "expressing boredom or frustration with the hanging installer.\n\n"
    "Since the project brief already lists several Extras, the best move is X."
)


def test_strips_the_two_live_leaks():
    assert strip(LIVE_1399).startswith("Given the **Running Recap**")
    assert "non-sequitur" not in strip(LIVE_1399)
    assert strip(LIVE_1404).startswith("Since the project brief")
    assert "expressing boredom" not in strip(LIVE_1404)


@pytest.mark.parametrize("meta", [
    "The operator is asking about the disk layout.\n\nUse lsblk.",
    "The user seems confused about boot order.\n\nRun qm config 106.",
    "Operator's message indicates a pivot.\n\nHere is the answer.",
])
def test_strips_other_narration_shapes(meta):
    assert strip(meta) != meta


@pytest.mark.parametrize("keep", [
    # ordinary content that merely BEGINS with the word — the first cut ate these
    "The operator error was in the config file.\n\nFix it like this.",
    "The user data directory is /var/lib/radarr.\n\nCheck it.",
    "Operator, run this command.\n\nThen report back.",
    # structure is content, never preamble
    "## Do this next\n\nrun it",
    "```bash\nqm config 106\n```\n\nthen paste it",
    "- The operator's note is recorded\n\nnext",
    # unrelated openers
    "Since the installer hangs, skip the update phase.\n\nDetail.",
])
def test_never_strips_legitimate_content(keep):
    assert strip(keep) == keep


def test_never_empties_the_message():
    """If the narration IS the whole message, stripping would leave nothing —
    a bad answer beats no answer (§17.876 honest-fallback contract)."""
    only = "The operator is confused about the boot order."
    assert strip(only) == only


def test_handles_empty_and_none():
    assert strip("") == ""
    assert strip(None) is None


def test_wired_into_every_operator_facing_surface():
    """Guide, fix and the ask/research answer all emit prose to the operator."""
    import inspect
    from app.modules import assist_guide, assist_research_lib
    guide_src = inspect.getsource(assist_guide)
    assert guide_src.count("strip_operator_meta_preamble(text_out)") == 2
    assert "strip_operator_meta_preamble(answer)" in inspect.getsource(assist_research_lib)
