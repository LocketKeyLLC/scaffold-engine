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

# ── §17.1340: the guest a step ACTS ON outranks the one it mentions ───────────
from app.modules import machine_truth as mt            # noqa: E402


def test_the_subject_is_the_guest_the_step_acts_on():
    """Live, ADD129's measured note names `container 101 (jellyfin)` before anything
    else and `pct set 103 -mp0 …` as the work. The subject came back 101 — the one
    container that already had the mount — and the step was drafted as work inside
    Jellyfin."""
    mentions_first = {"node_key": "ADD129", "title": "Give Radarr the shared media storage at /media",
                      "description": "Jellyfin (CT 101) mounts it read-only. Run `pct set 103 "
                                     "-mp0 /oasis/media,mp=/media` so Radarr can write."}
    assert mt.subject_guest(mentions_first) == "103", "the guest the command addresses"


def test_several_guests_acted_on_leave_no_single_subject():
    node = {"node_key": "ADD135", "title": "Make the machines start on boot",
            "description": "qm set 106 --onboot 1, pct set 111 --onboot 1, pct set 120 --onboot 1"}
    assert mt.subject_guest(node) is None, "it spans guests; there is nothing single to measure"
    assert rt.spans_guests(node) is True


def test_a_title_id_beats_a_description_id_when_no_command_decides():
    node = {"node_key": "X", "title": "Grow VM 106's filesystem",
            "description": "Earlier, container 101 was given the same treatment."}
    assert mt.subject_guest(node) == "106"


def test_the_name_route_still_works():
    """§17.1316 — T23 had no id in its text at all."""
    node = {"node_key": "T23", "title": "Install PalWorld server", "description": "Install it."}
    assert mt.subject_guest(node, {"names": {"106": "palworld-server"}}) == "106"


def test_one_subject_reader():
    """§17.1340 — `runbook_templates.subject_gid` used to search the text itself and
    drifted: the pause measured one guest and the template rendered another."""
    import inspect
    src = inspect.getsource(rt.subject_gid)
    assert "subject_guest" in src and "_SUBJECT_RE" not in src, src


def test_only_four_steps_in_the_live_plan_change_subject():
    """Measured: ADD115 and ADD116 gain a subject their commands name, ADD135 loses
    one because it spans, and ADD129 moves from the container it mentions to the one
    it acts on."""
    import re as _re
    SUB = _re.compile(r"\b(?:VM|CT|LXC|container|guest)\s*#?\s*(\d{3,5})\b", _re.I)
    changed = []
    for n in PLAN:
        text = f"{n['title']}\n{n['description'] or ''}"
        m = SUB.search(text)
        if (m.group(1) if m else None) != mt.subject_guest(n):
            changed.append(n["node_key"])
    assert set(changed) <= {"ADD115", "ADD116", "ADD135", "ADD129"}, changed
