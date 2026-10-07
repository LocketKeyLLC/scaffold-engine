r"""§17.1368 — a block running on the HOST reaching a guest's port on loopback.

§17.1358 judges the same mistake the other way round — a guest's port used INSIDE
a different guest — and finds the guest from `pct exec N --` on the line. So a
call with no such wrapper is invisible to it: a `curl` in a host-side script, or
`urllib.request.urlopen` in a Python file the host runs. Gate one end and the
other stays open.

Live, 2026-10-04. ADD132's ninth draft moved to Python and `urllib`, which
correctly solved §17.1367's quoting problem, and called

    url = f"http://127.0.0.1:{port}/api/v3/downloadclient"
    …
    radarr_client = get_download_client(103, radarr_key, 7878)

from a script the runner executes on the Proxmox host. The frame carried
`suggested: run` and **no refusals**. Measured:

    host -> 127.0.0.1:7878 = 000      host -> 127.0.0.1:8989 = 000
    the host's own `ss -tlnp` has nothing on those ports
    container 103 = 192.168.1.22      container 104 = 192.168.1.23

Two things were missing. The facts said *"port 7878 on guest 103"* and never said
**where guest 103 is**, so the only address the drafter had was loopback — the
measurement now reads `radarr on guest 103 (at 192.168.1.22) · port 7878 · …`.
And the judgment is over the whole BLOCK, because the loopback and the port are
on different lines: a line-at-a-time rule saw a loopback with no port and a port
with no loopback, and refused nothing.
"""
from __future__ import annotations

from app.modules.service_truth import ServiceTruth, loopback_on_the_host

RADARR = ServiceTruth(guest="103", unit="radarr.service", name="radarr", state="active",
                      ports=("7878",), address="192.168.1.22")
SONARR = ServiceTruth(guest="104", unit="sonarr.service", name="sonarr", state="active",
                      ports=("8989",), address="192.168.1.23")
QBIT = ServiceTruth(guest="105", unit="qbittorrent-nox.service", name="qbittorrent-nox",
                    state="active", ports=("61661", "8080"), address="192.168.1.24")
SERVICES = [RADARR, SONARR, QBIT]
#: the live script, reduced to the lines that matter
LIVE = ['url = f"http://127.0.0.1:{port}/api/v3/downloadclient"',
        "radarr_client = get_download_client(103, radarr_key, 7878)",
        "sonarr_client = get_download_client(104, sonarr_key, 8989)"]


# -------------------------------------------- the address is part of the fact


def test_the_measurement_now_says_where_the_guest_is():
    assert RADARR.says().startswith("radarr on guest 103 (at 192.168.1.22) · port 7878")
    # and without an address the line is exactly what it was
    bare = ServiceTruth(guest="103", unit="radarr.service", name="radarr", ports=("7878",))
    assert bare.says().startswith("radarr on guest 103 · port 7878")


# ------------------------------------------------------------- the gate


def test_the_live_script_is_refused_for_both_services():
    out = loopback_on_the_host(LIVE, [], SERVICES)
    assert len(out) == 2, [r["why"][:70] for r in out]
    whys = " ".join(r["why"] for r in out)
    assert "port 7878 on loopback" in whys and "port 8989 on loopback" in whys
    assert "radarr in guest 103" in whys and "sonarr in guest 104" in whys
    # the remedy names the measured address AND the in-guest alternative
    assert "Reach it at 192.168.1.22:7878" in whys
    assert "pct exec 103 -- sh -c '…'" in whys       # §17.1402 — exec_hint, kind-aware
    assert "answered `000`" in whys


def test_a_literal_port_on_the_loopback_line_is_enough():
    out = loopback_on_the_host(["curl -s http://127.0.0.1:8080/api/v2/app/version"], [], SERVICES)
    assert len(out) == 1
    assert "qbittorrent-nox in guest 105" in out[0]["why"]
    assert "192.168.1.24:8080" in out[0]["why"]


def test_a_call_inside_the_guest_is_not_this_mistake():
    """What §17.1358 owns, and what a correct draft does."""
    for ok in ("pct exec 103 -- sh -c 'curl -s http://127.0.0.1:7878/api/v3/health'",
               "qm guest exec 106 -- bash -c 'curl -s http://localhost:8080/'"):
        assert loopback_on_the_host([ok], [], SERVICES) == [], ok


def test_the_guests_real_address_is_fine():
    assert loopback_on_the_host(
        ["curl -s http://192.168.1.22:7878/api/v3/health"], [], SERVICES) == []


def test_a_loopback_port_nobody_measured_is_left_alone():
    assert loopback_on_the_host(["curl -s http://127.0.0.1:9999/"], [], SERVICES) == []
    assert loopback_on_the_host(["curl -s http://127.0.0.1:8000/health"], [], SERVICES) == []


def test_a_comment_is_not_a_call():
    assert loopback_on_the_host(["# curl -s http://127.0.0.1:7878/api/v3/health",
                                 "radarr_key = 'x'"], [], SERVICES) == []


def test_a_transcript_is_not_a_block():
    r"""T20's record is a session INSIDE container 105 —
    `root@download-client:~# … curl -I http://localhost:8080` — where that line
    was right. A prompt says the lines already ran, and where."""
    transcript = ("root@download-client:~# systemctl is-active qbittorrent-nox\nactive\n"
                  "root@download-client:~# curl -I http://localhost:8080\nHTTP/1.1 200 OK\n")
    assert loopback_on_the_host([transcript], [], SERVICES) == []


def test_nothing_measured_judges_nothing():
    assert loopback_on_the_host(LIVE, [], []) == []
    assert loopback_on_the_host(LIVE, [], [ServiceTruth(guest="103", unit="x", name="x")]) == []


def test_one_refusal_per_port():
    assert len(loopback_on_the_host(LIVE + LIVE, [], SERVICES)) == 2


def test_the_files_are_read_too():
    assert loopback_on_the_host([], [{"path": "/tmp/x.py", "content": "\n".join(LIVE)}], SERVICES)


# ------------------------------------------------------------- the lane


def test_the_framer_runs_it_beside_its_sibling():
    """§17.1358 and §17.1368 are the two ends of one mistake."""
    import inspect

    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr.frame_run)
    assert "_st.values_from_another_guest(cmds, files, services)" in src
    assert "_st.loopback_on_the_host(cmds, files, services)" in src


def test_the_refusal_asks_the_drafter_again():
    from app.modules.supervised_runs import refusal_kinds
    out = loopback_on_the_host(LIVE, [], SERVICES)
    assert "reaches port" in refusal_kinds({"refused": out})
