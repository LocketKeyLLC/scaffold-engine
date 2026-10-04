"""§17.1344 — an id no machine has is not a guest.

Live, 2026-10-04. The step that hands the shared media tree to the media services
carries the measurement that made it necessary:

    the services report uid 999 and gid 996, so in-container 999:996 is host
    100999:100996

`_SUBJECT_RE` reads `container|VM|CT|LXC|guest` followed by digits, so
`in-container 999` became **guest 999**. The frame then refused its own
host-side command:

    `chown -R 100999:100996 /oasis/media` runs in the runner's own shell on the
    Proxmox HOST, and the step is about VM/CT 999 — it would change the HOST
    instead of guest 999

Run was withheld and "I'll do it myself" suggested, for two correct commands on
the host. This host has guests 100 through 130; there is no 999, and the
inventory the pause already read says so.
"""
from __future__ import annotations

from app.modules import machine_truth as mt
from app.modules import runbook_preconditions as rp
from app.modules import supervised_runs as sr

#: `pct list` + `qm list` that pause, as the pause hands them over
INV = {"cts": {"101": "running", "103": "running", "104": "running", "105": "running",
               "111": "running", "120": "stopped", "130": "running"},
       "vms": {"100": "stopped", "106": "running", "110": "stopped"},
       "names": {"101": "jellyfin", "103": "radarr", "104": "sonarr", "105": "download-client",
                 "106": "palworld-server", "111": "control-panel", "120": "caddy-proxy", "130": "pihole"}}
#: the phrase that caused it, verbatim from the step
PHRASE = ("the services report uid 999 and gid 996, so in-container 999:996 is host 100999:100996. "
          "Run `chown -R 100999:100996 /oasis/media` on the host.")
PROSE_NODE = {"node_key": "ADD138", "title": "Give the shared media tree to the media services' own user",
              "description": PHRASE}


def test_the_phrase_really_reads_as_a_guest():
    """The pre-image: this is why the refusal appeared."""
    assert rp._SUBJECT_RE.findall(PHRASE) == ["999"], rp._SUBJECT_RE.findall(PHRASE)


def test_an_id_the_host_does_not_have_is_no_subject():
    assert mt.subject_guest(PROSE_NODE, INV) is None


def test_with_no_inventory_nothing_changes():
    """Blindness invents nothing and refuses nothing: the old reading stands when
    there is no listing to check against."""
    assert mt.subject_guest(PROSE_NODE) == "999"


def test_a_real_id_in_prose_is_still_the_subject():
    node = {"node_key": "X", "title": "Grow the filesystem", "description": "Inside container 103, run resize2fs."}
    assert mt.subject_guest(node, INV) == "103"


def test_the_first_KNOWN_id_wins_over_an_earlier_number():
    """The reader no longer stops at the first match: it takes the first match that
    names a machine this host has."""
    node = {"node_key": "Y", "title": "Hand the tree over",
            "description": "in-container 999:996 is host 100999:100996; then look in container 104."}
    assert mt.subject_guest(node, INV) == "104"


def test_an_id_this_block_creates_counts():
    node = {"node_key": "Z", "title": "Create container 121 for the new app",
            "description": "pct create 121 --hostname newapp --storage local-lvm"}
    assert mt.subject_guest(node, INV) == "121"
    assert mt.known_guest("121", INV, "pct create 121 --hostname newapp") is True


def test_known_guest_reads_both_listings_and_the_names():
    assert mt.known_guest("106", INV) is True          # a VM
    assert mt.known_guest("130", INV) is True          # a container
    assert mt.known_guest("999", INV) is False
    assert mt.known_guest("", INV) is False
    assert mt.known_guest("999", None) is True, "no inventory: no judgment"
    assert mt.known_guest("999", {"cts": {}, "vms": {}, "names": {}}) is True, "unreadable listings: no judgment"


#: the five commands the frame carried
LIVE_CMDS = ["chown -R 100999:100996 /oasis/media",
             "chmod -R 2775 /oasis/media",
             "pct exec 103 -- su -s /bin/sh -c 'touch /media/movies/.w && rm /media/movies/.w' radarr",
             "pct exec 104 -- su -s /bin/sh -c 'touch /media/tv/.w && rm /media/tv/.w' sonarr",
             "pct exec 105 -- su -s /bin/sh -c 'touch /media/downloads/.w && rm /media/downloads/.w' qbittorrent-nox"]


def test_the_refusal_reproduces_without_the_inventory():
    """The pre-image, through the rule that actually wrote it
    (`commands_never_reach_the_guest`, §17.1285): the host-side `chown` is refused
    for not reaching "guest 999"."""
    out = sr.commands_never_reach_the_guest(LIVE_CMDS, PROSE_NODE)
    assert len(out) == 1, out
    assert "guest 999" in out[0]["why"] and out[0]["command"].startswith("chown -R 100999:100996")


def test_the_inventory_settles_it_and_the_refusal_goes():
    assert sr.commands_never_reach_the_guest(LIVE_CMDS, PROSE_NODE, None, INV) == []


def test_the_rule_still_bites_where_it_was_earned():
    """§17.1285's own live case: ADD82 would have installed the guest agent on the
    hypervisor. VM 106 is a machine this host HAS, so nothing changes for it."""
    add82 = {"title": "Install and enable QEMU Guest Agent in VM 106",
             "description": "Inside VM 106, install the agent."}
    out = sr.commands_never_reach_the_guest(["apt-get install -y qemu-guest-agent"], add82, None, INV)
    assert len(out) == 1 and "guest 106" in out[0]["why"], out


def test_the_frame_passes_the_inventory_to_the_rule():
    """Verify the lane: a filter the caller never feeds is no filter."""
    import inspect
    body = inspect.getsource(sr.frame_run)
    assert "commands_never_reach_the_guest(cmds, node, shape_files, inventory)" in body, body[-600:]
