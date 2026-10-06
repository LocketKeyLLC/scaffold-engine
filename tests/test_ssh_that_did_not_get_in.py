"""§17.1393 — an ssh that did not get in is a contradiction, not "unknown".

Live, 2026-10-06. ADD137 — "Give the control panel an ssh key it can USE on VM
106" — installed the key and then ran the one check that proves the point:

    $ pct exec 111 -- ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new \\
          aedefruscio@192.168.1.106 true
    @    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @
    IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!

ssh refused and exited 255. The model judge returned `unknown`, §17.1233's
asymmetry (only a CONTRADICTED verdict downgrades) let it stand, and the step was
recorded done with "2 command(s) ran, all exited 0" — the panel cannot use the key
the step exists to give it.

`negative_evidence` is the deterministic pre-pass that runs before the model and
already settles curl errors, `systemctl is-active` and missing paths — its own
comment cites §17.1310, where the model called "Cannot GET /api/health" unknown
and a dead step stood as done. ssh simply never had a branch.
"""
from __future__ import annotations

import pytest

from app.modules.assist_state_check import negative_evidence

LIVE_CMD = ("pct exec 111 -- ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
            "aedefruscio@192.168.1.106 true")
LIVE_OUT = ("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
            "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
            "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
            "IT IS POSSIBLE THAT SOMEONE IS DOING SOMETHING NASTY!")


def test_the_live_add137_check_is_contradicted():
    why = negative_evidence(LIVE_CMD, LIVE_OUT)
    assert why and "ssh did not get in" in why
    assert "REMOTE HOST IDENTIFICATION HAS CHANGED" in why


@pytest.mark.parametrize("out", [
    "Host key verification failed.",
    "aedefruscio@192.168.1.106: Permission denied (publickey).",
    "ssh: connect to host 192.168.1.106 port 22: Connection refused",
    "ssh: connect to host 192.168.1.106 port 22: Connection timed out",
    "ssh: connect to host 192.168.1.106 port 22: No route to host",
    "ssh: Could not resolve hostname palworld: Name or service not known",
    "Received disconnect from 192.168.1.106: Too many authentication failures",
])
def test_every_way_ssh_says_no_is_contradicted(out):
    assert negative_evidence("ssh -o BatchMode=yes u@192.168.1.106 true", out)


def test_inside_a_container_exec_too():
    """The live shape: the ssh runs inside `pct exec 111 --`."""
    assert negative_evidence(LIVE_CMD, "Permission denied (publickey).")


@pytest.mark.parametrize("cmd,out", [
    (LIVE_CMD, ""),                                       # a successful `ssh … true` prints nothing
    (LIVE_CMD, "Warning: Permanently added '192.168.1.106' (ED25519) to the list of known hosts."),
    ("cat /etc/motd", "REMOTE HOST IDENTIFICATION HAS CHANGED"),   # not an ssh check
    ("grep -c ssh /etc/services", "Connection refused"),           # mentions ssh, isn't one
])
def test_what_it_must_not_claim(cmd, out):
    assert negative_evidence(cmd, out) == ""


def test_a_curl_still_goes_through_the_curl_branch():
    """The phrase "Connection refused" belongs to both; the curl branch must
    still own a curl, with its own wording."""
    why = negative_evidence("curl -s http://192.168.1.20:8096/", "curl: (7) Connection refused")
    assert why and "answered an error" in why and "ssh" not in why


def test_accept_new_first_contact_is_not_a_failure():
    """`StrictHostKeyChecking=accept-new` on a host never seen before prints
    "Permanently added" and succeeds. Only a CHANGED key is refused."""
    assert negative_evidence(LIVE_CMD, "Warning: Permanently added '192.168.1.106'") == ""
