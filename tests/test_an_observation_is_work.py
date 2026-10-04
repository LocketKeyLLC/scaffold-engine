"""§17.1354 — a step whose result is an observation of a machine is hands-on.

Live, 2026-10-04. ADD134 — *"Prove the whole chain: ask for one title and watch
it arrive in Jellyfin"* — ran as a model task and was failed by the writer gate:

    this step produced INSTRUCTIONS, not work. It wrote 24 commands for a
    machine the engine can reach

§17.1349's fallback asks whether the title opens with a verb that CHANGES
something (`add`, `set`, `install`, …) and names a service the engine knows.
"Prove" changes nothing, so nothing said "work" — and a model cannot observe a
machine, so the step could only ever come back as prose.

Measured over all 169 steps of the live plan: adding an observation verb to that
fallback — with the service named in the TITLE, as §17.1349's own measurement
requires, and a criterion only the machine can settle — flags exactly two steps,
ADD134 and ADD98 (the same end-to-end proof an arc earlier), and leaves every
other step's verdict untouched.
"""
from __future__ import annotations

import json
import pathlib

from app.modules.step_classify import step_is_hands_on

STEPS = {s["node_key"]: s for s in json.loads(
    (pathlib.Path(__file__).parent / "fixtures"
     / "plan_steps_observation_2026_10_04.json").read_text())}


def _on(key: str):
    return step_is_hands_on({k: STEPS[key][k] for k in ("tool", "title", "description")})


def test_the_step_that_failed_as_a_model_task_is_hands_on_now():
    on, why = _on("ADD134")
    assert on, STEPS["ADD134"]["title"]
    assert why == "observe:prove service:jellyfin", why


def test_the_same_proof_an_arc_earlier_is_hands_on_too():
    """ADD98, whose done-when is "you can play that film from Jellyfin"."""
    on, why = _on("ADD98")
    assert on and why == "observe:prove service:jellyfin", why


def test_a_change_verb_still_gives_the_more_precise_reason():
    """The fallback is a fallback. Both live steps come back hands-on for a
    reason that is NOT this rule's: ADD132's own description carries a `curl`
    that writes, and ADD133's title opens with `Add` and names Jellyfin."""
    for key in ("ADD132", "ADD133"):
        on, why = _on(key)
        assert on, key
        assert not why.startswith("observe:"), (key, why)
    assert _on("ADD132")[1].startswith("writes:")
    # the change-verb branch itself, with no command in the text to outrank it
    assert step_is_hands_on({
        "title": "Make Radarr drive qBittorrent",
        "description": "Point it at the download client."}) == (True, "verb:make service:radarr")


def test_the_model_steps_in_the_same_plan_are_still_model_steps():
    for key in ("T38", "ADD95", "ADD101", "ADD96", "ADD113"):
        on, why = _on(key)
        assert not on, f"{key} ({STEPS[key]['title']}) became hands-on: {why}"


def test_an_observation_with_no_service_the_engine_knows_is_not_work():
    """The engine measures services; a step about something else is not this rule's."""
    assert step_is_hands_on({
        "title": "Verify the plan covers every capability the operator asked for",
        "description": "Done when each of the four capabilities has a step."}) == (False, "")


def test_an_observation_with_no_criterion_a_machine_can_settle_is_not_work():
    """A service in the title is not enough: the step must be settled by a reading."""
    assert step_is_hands_on({
        "title": "Review how Jellyfin is organised",
        "description": "Write up how the libraries are laid out and why."}) == (False, "")


def test_each_observation_verb_counts():
    for verb in ("Prove", "Confirm", "Verify", "Check", "Test", "Watch", "Measure", "Observe"):
        on, why = step_is_hands_on({
            "title": f"{verb} that Jellyfin serves the new library",
            "description": "Done when the API lists it."})
        assert on, verb
        assert why == f"observe:{verb.lower()} service:jellyfin", why


def test_a_guest_prefix_on_the_title_does_not_hide_the_verb():
    on, why = step_is_hands_on({
        "title": "LXC 101: prove Jellyfin answers after the restart",
        "description": "Done when the API returns 200."})
    assert on and why == "observe:prove service:jellyfin", why


def test_a_command_in_the_text_still_outranks_the_fallback():
    """The fallback is a fallback: a command that writes gives the precise reason."""
    on, why = step_is_hands_on({
        "title": "Prove Jellyfin answers",
        "description": "Done when `systemctl restart jellyfin` leaves it active."})
    assert on and why != "observe:prove service:jellyfin", why
