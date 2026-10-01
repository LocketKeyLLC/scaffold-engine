"""§17.1253 — the classifier could not tell a forbidden command from a performed one.

`step_is_hands_on` reads a step's description for commands and treats any write
as proof the step does host work. A correction written into a description is
almost always phrased as a prohibition, so correcting a PROSE step turned it into
a supervised one.

Live, ADD112 ("Point the Spectrum router's DNS at Pi-hole") is work the operator
does in an app on their phone, and it had been producing a walkthrough correctly.
A note was then added saying

    DO NOT ENABLE PI-HOLE'S DHCP SERVER IN THIS STEP. The previous draft ended
    with `pct exec 130 -- /usr/local/bin/pihole -a enabledhcp …`

and that one quoted command flipped the step to hands-on: instead of a
walkthrough it was claimed, handed back for approval, and the operator got
nothing. The sentence forbids the command; the engine read it as the job.
"""
from __future__ import annotations

import pytest

from app.modules.step_classify import forbidden_for, step_is_hands_on


def _node(description: str, **kw):
    return {"title": kw.pop("title", "Point the router's DNS at Pi-hole"),
            "tool": "LLM", "description": description, **kw}


@pytest.mark.parametrize("text", [
    "DO NOT ENABLE DHCP. The previous draft ended with `pct exec 130 -- pihole -a enabledhcp`.",
    "Do NOT run `pct exec 130 -- pihole -a enabledhcp`; it would add a second DHCP server.",
    "The previous attempt ran `pct create 130 local:vztmpl/x` and it collided.",
    "Never write `pct destroy 130` in this step.",
    "Avoid `qm set 106 --scsi0 x` — it shrinks the disk.",
    "This step cannot use `pct exec 106 -- apt-get install` because 106 is a VM.",
    "Use the app instead of `pct set 130 --nameserver 1.1.1.1`.",
])
def test_a_command_named_to_rule_it_out_does_not_make_a_step_hands_on(text):
    on, why = step_is_hands_on(_node(text))
    assert on is False, f"{why} — a forbidden command is not the work"


@pytest.mark.parametrize("text", [
    "Run `pct start 130` and confirm it comes up.",
    "Start the container with `pct start 130`.",
    "Set the memory with `pct set 130 --memory 1024`.",
])
def test_a_command_the_step_actually_performs_still_counts(text):
    on, why = step_is_hands_on(_node(text))
    assert on is True, text


def test_a_fenced_command_is_never_excused():
    """The step's actual work lives in fences, and `step_commands` gives fenced
    lines an empty sentence — so no prohibition anywhere in the prose can excuse
    them."""
    on, _ = step_is_hands_on(_node(
        "Do not do the old thing, and never run `pct destroy 130`.\n\n"
        "```bash\npct set 130 --memory 1024\n```"))
    assert on is True


def test_the_prohibition_must_come_BEFORE_the_command():
    """"Done when `ip -brief link show veth105i0` reports master vmbr0 (no longer
    fwbr105i0)" describes the expected END STATE. The first version read that
    trailing negative as a prohibition and turned a real hands-on step into
    prose — the direction that loses work."""
    cmd = "ip -brief link show veth105i0"
    after = f"Done when '{cmd}' reports master vmbr0 (no longer fwbr105i0)."
    assert not forbidden_for(after, cmd)
    before = f"Do NOT run `{cmd}` in this step."
    assert forbidden_for(before, cmd)


def test_forbidden_for_is_about_prohibition_not_mood():
    cmd = "pct start 130"
    for yes in (f"Do not run `{cmd}`.", f"Never run `{cmd}`.",
                f"The previous attempt ran `{cmd}`.", f"Avoid `{cmd}`.",
                f"It must not run `{cmd}`.", f"Out of scope for this step: `{cmd}`."):
        assert forbidden_for(yes, cmd), yes
    for no in ("", f"Run `{cmd}` now.", f"Start it with `{cmd}`.",
               f"The container is not running yet, so run `{cmd}`."):
        assert not forbidden_for(no, cmd), no


def test_the_classifier_consults_it_on_both_signals():
    """Writes AND observed-completion both read sentences, so both must skip a
    forbidden one."""
    import inspect
    src = inspect.getsource(step_is_hands_on)
    assert src.count("forbidden_for(sentence, cmd)") == 2


def test_a_tool_tag_still_wins():
    """§17.1183's tag rules come first — a Shell step is hands-on whatever its
    prose says."""
    on, why = step_is_hands_on({"title": "t", "tool": "Shell",
                                "description": "Do not run `pct start 130`."})
    assert on is True and why == "tool:shell"
