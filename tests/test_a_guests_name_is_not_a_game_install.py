"""§17.1339 — a guest's own name does not make a step a game install.

Live, 2026-10-04. A step was added so the panel could reach the game server:
"Install the control panel's public key on VM 106 (palworld-server)". The pause
framed it from `INSTALL_STEAM_SERVER` — a 4.6 GB SteamCMD install that creates a
`steam` user, installs app 2394010 and rewrites `palworld.service` — with **zero
refusals and Run suggested**, and a check (`ls -la /opt/palworld/*.sh`) that has
nothing to do with an ssh key. Approving it would have reinstalled the game server
and recorded the key step done, leaving the panel to fail with publickey the first
time anyone opened the page.

`_STEAM_TITLE_RE` wanted `install … <game> … server`. The guest is NAMED
`palworld-server`, so its name supplied both halves. The comment above that
selector already said a step that merely mentions the guest is not a game install;
the guard had been moved from the description to the title, and the title names the
guest too.
"""
from __future__ import annotations

import json
import pathlib

from app.modules import runbook_templates as rt

FX = pathlib.Path(__file__).parent / "fixtures"
#: every step of the live home-lab plan, as stored
PLAN = json.loads((FX / "homelab_plan_host_side_2026_10_03.json").read_text(encoding="utf-8"))
#: the title that selected the game template, from the node's own audit row
LIVE_TITLE = "Install the control panel's public key on VM 106 (palworld-server)"


def test_the_title_that_caused_this_is_not_a_game_install():
    assert rt.names_a_game_install(LIVE_TITLE) is False


def test_the_real_game_installs_still_are():
    for title in ("Install PalWorld server",
                  "Install the PalWorld dedicated server (Steam app 2394010) inside VM 106",
                  "Set up a Valheim dedicated server on VM 107",
                  "Install SteamCMD and deploy the server"):
        assert rt.names_a_game_install(title) is True, title


def test_the_templates_own_title_matches_its_selector():
    """A template that cannot select its own rendered title is a template that
    never applies twice."""
    rendered = rt.INSTALL_STEAM_SERVER.title.format(GAME="PalWorld", APP_ID="2394010", GID="106")
    assert rt.names_a_game_install(rendered) is True, rendered


def test_a_hyphenated_name_is_a_name_not_two_words():
    assert rt.names_a_game_install("Start palworld-server and check UDP 8211") is False
    assert rt.names_a_game_install("Resize the palworld-server disk to 100G") is False


def test_the_steps_object_settles_it():
    """A key, a driver, an agent, a certificate: not a game server, however the
    guest is named."""
    for title in ("Install the control panel's public key on VM 106 (palworld-server)",
                  "Install and enable QEMU Guest Agent in VM 106 (palworld-server)",
                  "Install the NVIDIA driver on the palworld-server VM",
                  "Install the TLS certificate for the palworld-server host",
                  "Install the nginx reverse proxy in front of the palworld server"):
        assert rt.names_a_game_install(title) is False, title


def test_nothing_else_in_the_live_plan_is_called_a_game_install():
    """Measured over the stored plan: one step installs the game server."""
    hits = [n["node_key"] for n in PLAN if rt.names_a_game_install(n["title"] or "")]
    assert hits == ["T23"], hits


def test_the_template_consults_the_one_reader():
    """Verify the lane: the selector goes through `names_a_game_install`, so the
    object guard cannot be bypassed by a second call site."""
    import inspect
    src = inspect.getsource(rt)
    assert src.count("_STEAM_TITLE_RE.search") == 1, "one reader"
    assert "names_a_game_install(str((node or {}).get(\"title\") or \"\"))" in src

#: the live steps, as stored: one spans two guests, one spans three, one spans none
SPANNING = json.loads((FX / "steps_that_span_guests_2026_10_04.json").read_text(encoding="utf-8"))


def test_the_key_step_spans_two_guests_so_no_guest_template_claims_it():
    """§17.1339b — it reads the key from container 111 and appends it on VM 106.
    Its words ("service", "inside the container") made the in-guest reader fire and
    sent it to a single-guest template, which drafted work in the wrong machine."""
    node = SPANNING["ADD137"]
    assert rt.guests_the_host_is_told_to_touch(node) == {"106", "111"}
    assert rt.spans_guests(node) is True
    assert rt.host_side_only(node) is True
    assert rt.intent_of(node) is None, "the model path with host context, not a guest template"


def test_the_boot_flag_step_spans_three():
    node = SPANNING["ADD135"]
    assert rt.guests_the_host_is_told_to_touch(node) == {"106", "111", "120"}
    assert rt.host_side_only(node) is True


def test_a_step_about_one_guest_is_untouched():
    """§17.1329 keeps its lesson: a step naming a start AND in-guest work is fully
    served by the guest template, which starts its guest first."""
    node = SPANNING["ADD66"]
    assert len(rt.guests_the_host_is_told_to_touch(node)) <= 1
    assert rt.spans_guests(node) is False


def test_one_host_command_is_not_a_span():
    node = {"node_key": "X", "title": "Run the migration in container 111",
            "description": "pct exec 111 -- bash -c 'systemctl restart control-panel'"}
    assert rt.spans_guests(node) is False
    assert rt.host_side_only(node) is False, "one guest: the container template owns it"


def test_only_the_live_multi_guest_step_spans_in_the_whole_plan():
    hits = sorted(n["node_key"] for n in PLAN if rt.spans_guests(n))
    assert hits == ["ADD135"], hits
    singles = [n for n in PLAN if len(rt.guests_the_host_is_told_to_touch(n)) == 1]
    assert len(singles) > 20, "the common case stays the common case"
