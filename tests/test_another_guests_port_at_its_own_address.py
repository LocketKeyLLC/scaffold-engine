r"""§17.1358 — another guest's port, reached at that guest's address, is right.

Live, 2026-10-04, ADD132's fourth draft — the first one that was correct all the
way through. It computes qBittorrent's PBKDF2 hash itself, stops the service
before editing its config, inserts the keys INSIDE `[Preferences]` with a `sed`
range on the header (§17.1343), restarts, proves the login, reads each app's API
key out of its own `config.xml`, and registers the download client with both:

    pct exec 103 -- sh -c 'curl … -d "{…\"host\":\"<QBITTORRENT_IP>\",\"port\":8080,…}" http://127.0.0.1:7878/api/v3/downloadclient/1'
    pct exec 104 -- sh -c 'curl … -d "{…\"host\":\"<QBITTORRENT_IP>\",\"port\":8080,…}" http://127.0.0.1:8989/api/v3/downloadclient/1'

§17.1346 refused both: *"this runs inside guest 103, and 8080 belongs to
qbittorrent-nox on guest 105"*. But telling Radarr where the download client is
IS the step — the gate refused the one thing it exists to do.

The defect §17.1346 was built for was `127.0.0.1:8989` inside container 103:
a claim that Sonarr is HERE. So the discriminator is the HOST beside the port —
loopback or no host at all is a local claim and still wrong; a real address, or a
placeholder for one, is a remote claim and right. And the *arr APIs state it as
two JSON fields in an escaped, shell-quoted body, which is the shape that has to
be read.
"""
from __future__ import annotations

import json
import pathlib

from app.modules import service_truth as st
from app.modules.service_truth import ServiceTruth, values_from_another_guest

LIVE = json.loads((pathlib.Path(__file__).parent / "fixtures"
                   / "add132_cross_guest_calls_2026_10_04.json").read_text())
#: the three services exactly as §17.1356 measures them
SERVICES = [
    ServiceTruth(guest="103", unit="radarr.service", name="radarr", state="active",
                 ports=("7878",), data_dir="/var/lib/radarr"),
    ServiceTruth(guest="104", unit="sonarr.service", name="sonarr", state="active",
                 ports=("8989",), data_dir="/var/lib/sonarr"),
    ServiceTruth(guest="105", unit="qbittorrent-nox.service", name="qbittorrent-nox",
                 state="active", ports=("61661", "8080"),
                 data_dir="/var/lib/qbittorrent-nox"),
]


# --------------------------------------------------- the host beside the port


def test_a_loopback_or_bare_port_is_a_local_claim():
    assert not st._reaches_another_host("curl http://127.0.0.1:8989/api/v3/rootfolder", "8989")
    assert not st._reaches_another_host("curl http://localhost:8080/api/v2/app/version", "8080")
    assert not st._reaches_another_host("qbittorrent-nox --webui-port 8080", "8080")
    assert not st._reaches_another_host("ss -tlnp | grep :8080", "8080")


def test_a_real_address_or_a_placeholder_is_a_remote_claim():
    assert st._reaches_another_host("curl http://192.168.1.24:8080/api/v2/app/version", "8080")
    assert st._reaches_another_host("curl http://download-client:8080/api", "8080")
    assert st._reaches_another_host("curl http://<QBITTORRENT_IP>:8080/api/v2/auth/login", "8080")


def test_the_json_fields_the_arr_apis_use_are_read():
    """`\\"host\\":\\"…\\",\\"port\\":8080` inside a shell-quoted `-d` body."""
    assert st._reaches_another_host(
        r'-d "{\"host\":\"<QBITTORRENT_IP>\",\"port\":8080}"', "8080")
    assert st._reaches_another_host(
        r'-d "{\"host\":\"192.168.1.24\",\"port\":8080}"', "8080")
    assert not st._reaches_another_host(
        r'-d "{\"host\":\"127.0.0.1\",\"port\":8080}"', "8080")


def test_another_services_port_in_the_same_body_is_not_this_port():
    """The port must be the one being judged."""
    assert not st._reaches_another_host(
        r'-d "{\"host\":\"192.168.1.24\",\"port\":9117}"', "8080")


# ------------------------------------------------------------- the gate


def test_the_live_draft_is_no_longer_refused():
    assert len(LIVE["lines"]) == 2
    assert values_from_another_guest(LIVE["lines"], [], SERVICES) == []


def test_the_defect_the_gate_was_built_for_is_still_refused():
    """§17.1346's live case: both calls inside 103, Sonarr's at `127.0.0.1:8989`."""
    seg = ("pct exec 103 -- sh -c 'curl -s -H \"X-Api-Key: $K\" "
           "http://127.0.0.1:8989/api/v3/rootfolder'")
    out = values_from_another_guest([seg], [], SERVICES)
    assert len(out) == 1, out
    assert "8989 belongs to sonarr on guest 104" in out[0]["why"]


def test_another_guests_data_dir_is_still_refused():
    """A PATH has no address to make it remote: it is simply not there."""
    seg = "pct exec 103 -- sh -c 'cat /var/lib/sonarr/config.xml'"
    out = values_from_another_guest([seg], [], SERVICES)
    assert len(out) == 1, out
    assert "/var/lib/sonarr" in out[0]["why"]


def test_a_services_own_port_in_its_own_guest_is_fine():
    seg = "pct exec 103 -- sh -c 'curl -s http://127.0.0.1:7878/api/v3/downloadclient'"
    assert values_from_another_guest([seg], [], SERVICES) == []


def test_the_written_files_are_judged_the_same_way():
    files = [{"path": "/tmp/x.sh",
              "content": "pct exec 103 -- sh -c 'curl http://127.0.0.1:8080/api/v2/app/version'\n"}]
    assert values_from_another_guest([], files, SERVICES)
    ok = [{"path": "/tmp/x.sh",
           "content": "pct exec 103 -- sh -c 'curl http://192.168.1.24:8080/api/v2/app/version'\n"}]
    assert values_from_another_guest([], ok, SERVICES) == []


def test_nothing_measured_judges_nothing():
    assert values_from_another_guest(LIVE["lines"], [], []) == []
