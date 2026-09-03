"""§17.908 — a bolded instruction is a prescription, and reversing one silently
is the same defect as re-issuing a dead command.

Live (session 613dd1df, T23): turn 1362 said **Select "Ubuntu Server" (the full
version), NOT the minimized version.**; turn 1382 said **Select "Ubuntu Server
(minimized)"** — a straight reversal, unacknowledged. §17.898 fed only fenced
COMMANDS back to the model, so the advice was invisible to it.
"""
from __future__ import annotations

import pytest

from app.modules.assist_agent import _BOLD_DIRECTIVE_RE as RE

pytestmark = pytest.mark.asyncio

LIVE_1362 = '**Select "Ubuntu Server" (the full version), NOT the minimized version.**'


def test_captures_the_live_reversed_recommendation():
    assert RE.findall(LIVE_1362) == [
        'Select "Ubuntu Server" (the full version), NOT the minimized version.']


@pytest.mark.parametrize("bold,expected", [
    ("**Choose NO when asked about updates**", True),
    ("**Do NOT select anything.**", True),
    ("**Disable the security-update step**", True),
    ("**Skip the openssh-server option**", True),
    # labels and bare emphasis are not prescriptions
    ("**Type of Install:**", False),
    ("**do not**", False),
    ("**Diagnosis**", False),
    ("**Note:**", False),
])
def test_labels_and_bare_emphasis_are_not_prescriptions(bold, expected):
    from app.modules.assist_agent import _MIN_DIRECTIVE_CHARS
    hits = [h for h in RE.findall(bold)
            if len(" ".join(h.split())) >= _MIN_DIRECTIVE_CHARS]
    assert bool(hits) is expected, bold


def test_type_and_enter_are_excluded_as_ambiguous():
    """'Type of Install:' is a heading; 'type' is a noun here as often as a verb.
    Including it produced label noise on the live step."""
    assert RE.findall("**Type of Install:**") == []
    assert RE.findall("**Enter key mappings**") == []


def test_commands_and_directives_have_separate_budgets():
    """REGRESSION — one shared `out[:20]` cap let commands from the newest turns
    crowd out prose from older ones. On the live step the contradicted
    recommendation sat 14 turns back, comfortably inside the row window, but
    every slot was spent before the loop reached it. Third instance of this
    shape (cf. §17.906's cmds[:10] and the LIMIT 12 row window)."""
    import inspect
    from app.modules.assist_agent import _prescribed_commands
    src = inspect.getsource(_prescribed_commands)
    assert "cmds[:14] + directives[:10]" in src
    assert "out[:20]" not in src
    assert "LIMIT 40" in src  # window widened so earlier advice survives
