"""§17.925/926 — work the step's definition of done, not the nearest error.

Live (session 613dd1df, ADD5). The step reads:

    "The step is complete when the VM boots from its local disk into a working
     login prompt and is reachable via SSH from the Proxmox host."

The operator reported the login prompt at turn 1460 — half the criteria met,
the other half being SSH. The engine then spent SIX CONSECUTIVE replies on
`qemu-guest-agent`, a package the criteria never mention, and never once
proposed `openssh-server`. Each reply was locally reasonable; the sequence went
nowhere. The operator: "the continuation is just a repeat that will not achieve
anything nor solve the problem".
"""
from __future__ import annotations

import inspect

import pytest

from app.modules.assist_guide import extract_acceptance_criteria, find_goal_drift

pytestmark = pytest.mark.asyncio

ADD5 = ("Install Ubuntu Server 22.04 on VM 106 using the ISO. The step is "
        "complete when the VM boots from its local disk into a working login "
        "prompt and is reachable via SSH from the Proxmox host.")


def test_extracts_the_live_definition_of_done():
    c = extract_acceptance_criteria(ADD5)
    assert c.startswith("The step is complete when")
    assert "SSH" in c and "login prompt" in c


@pytest.mark.parametrize("phrasing", [
    "Done when the service responds on port 8080.",
    "Success is: the container starts and stays up.",
    "Acceptance criteria: the disk is mounted at /srv.",
])
def test_recognises_other_definition_phrasings(phrasing):
    assert extract_acceptance_criteria(phrasing)


def test_no_criteria_yields_empty_not_a_guess():
    assert extract_acceptance_criteria("Install the package.") == ""
    assert extract_acceptance_criteria(None) == ""


def test_topic_drift_detector_is_documented_as_unreliable():
    """§17.926 — `find_goal_drift` tests whether recent replies MENTION a
    criteria term. That bar is meaningless here: the criteria contain "vm",
    "disk", "host", "prompt" — words in nearly every reply — so it did NOT fire
    on the live six-turn yak-shave it was written for. Kept for the cases it
    does catch, but the STALL signal (turn count) is what the prompt uses."""
    c = extract_acceptance_criteria(ADD5)
    # squarely off-goal replies with none of the criteria vocabulary
    assert find_goal_drift(["install qemu-guest-agent"] * 4, c) is True
    # and the false-negative that motivated the switch
    assert find_goal_drift(
        ["the vm disk on the proxmox host"] * 4, c) is False


def test_stall_uses_turn_count_and_is_valved():
    from app.config import settings
    from app.modules import assist_guide
    assert 2 <= settings.assist_stalled_step_replies <= 20
    src = inspect.getsource(assist_guide.generate_fix)
    assert "failure_streak >= settings.assist_stalled_step_replies" in src
    assert "STOP — this step has stalled" in src


def test_stall_notice_is_surfaced_to_the_operator():
    """The model was told to re-anchor and continued its thread anyway. Prompt
    rules are guidance — so the stall, the definition of done, and the two ways
    out are shown to the OPERATOR, whose judgement this actually is."""
    from app.modules import assist_guide
    src = inspect.getsource(assist_guide.generate_fix)
    assert "Still on this step after" in src
    assert "different approach" in src


def test_stall_notice_lives_in_generate_fix_not_generate_guidance():
    """REGRESSION — it was first inserted at the wrong `promote_inline_commands`
    site, inside generate_guidance, where `_stalled` and `failure_streak` do not
    exist: a NameError on every Guide press. The probe in use at the time only
    exercised generate_fix, so nothing caught it."""
    from app.modules import assist_guide
    assert "Still on this step after" not in inspect.getsource(
        assist_guide.generate_guidance)
    assert "Still on this step after" in inspect.getsource(assist_guide.generate_fix)


def test_look_gate_is_exempt_in_a_console():
    """§17.907 exists to stop blind guessing. In a console with no copy-paste it
    was forcing `cat /etc/apt/sources.list` + "paste the output here" instead of
    a direct one-line fix the operator could type — looking is not free there."""
    from app.modules import assist_guide
    src = inspect.getsource(assist_guide.generate_fix)
    assert "_block_runs_on_root_host(draft, draft.find" in src
