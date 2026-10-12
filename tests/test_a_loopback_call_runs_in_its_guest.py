r"""§17.1502 — a host-side call to a guest's port on loopback is wrapped into that guest.

Live, job c75d77fa T5v2 "Verify testable" (2026-10-12). The step drafted
`curl -s "http://localhost:9696/api/v1/indexer" -H "X-Api-Key: $(cat
/var/lib/prowlarr/config.xml | grep …)"` as a HOST command. Prowlarr listens inside
guest 102, not on the host loopback, so `loopback_on_the_host` (§17.1368) refused it.
The engine measured which guest owns port 9696, so it repairs the target: the whole
command runs inside guest 102, where both the port and the config live.
"""
from __future__ import annotations

import subprocess

from app.modules.service_truth import (
    ServiceTruth,
    send_loopback_into_its_guest,
    loopback_on_the_host,
)

PROWLARR = ServiceTruth(name="prowlarr", guest="102", address="192.168.1.21", ports=("9696",))
RADARR = ServiceTruth(name="radarr", guest="103", address="192.168.1.22", ports=("7878",))
SERVICES = [PROWLARR, RADARR]

T5V2 = ("curl -s -X GET \"http://localhost:9696/api/v1/indexer\" "
        "-H \"X-Api-Key: $(cat /var/lib/prowlarr/config.xml | grep -oP '(?<=<ApiKey>)[^<]+')\" | head -c 2000")


def test_the_live_host_side_loopback_is_wrapped_into_guest_102():
    cmds, _, notes = send_loopback_into_its_guest([T5V2], [], SERVICES)
    out = cmds[0]
    assert out.startswith("pct exec 102 -- sh -c '")
    assert "localhost:9696" in out and "/var/lib/prowlarr/config.xml" in out
    assert notes and "§17.1502" in notes[0]["why"]


def test_the_wrapped_command_is_valid_shell():
    out = send_loopback_into_its_guest([T5V2], [], SERVICES)[0][0]
    r = subprocess.run(["bash", "-n", "-c", out], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_loopback_gate_refuses_the_original_and_is_quiet_after_the_wrap():
    assert loopback_on_the_host([T5V2], [], SERVICES), "the host-side loopback must be refused first"
    wrapped = send_loopback_into_its_guest([T5V2], [], SERVICES)[0]
    assert loopback_on_the_host(wrapped, [], SERVICES) == [], "nothing host-side remains after the wrap"


def test_a_call_already_inside_a_guest_is_left_alone():
    inside = "pct exec 102 -- sh -c 'curl -s http://localhost:9696/api/v1/indexer'"
    out, _, notes = send_loopback_into_its_guest([inside], [], SERVICES)
    assert out == [inside] and not notes


def test_a_call_to_the_guest_ip_is_left_alone():
    # 192.168.1.21:9696 is the correct host-side form; not loopback, not wrapped.
    direct = 'curl -s http://192.168.1.21:9696/api/v1/indexer'
    out, _, notes = send_loopback_into_its_guest([direct], [], SERVICES)
    assert out == [direct] and not notes


def test_a_loopback_port_no_guest_owns_is_left_alone():
    cmd = 'curl -s http://127.0.0.1:5555/x'
    out, _, notes = send_loopback_into_its_guest([cmd], [], SERVICES)
    assert out == [cmd] and not notes


def test_two_different_guests_ports_are_ambiguous_and_left_alone():
    cmd = 'curl http://localhost:9696/a && curl http://localhost:7878/b'
    out, _, notes = send_loopback_into_its_guest([cmd], [], SERVICES)
    assert out == [cmd] and not notes


def test_checks_are_wrapped_too():
    _, verify, notes = send_loopback_into_its_guest([], [T5V2], SERVICES)
    assert verify[0].startswith("pct exec 102 -- sh -c '") and notes


def test_frame_run_wraps_before_the_loopback_gate():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "send_loopback_into_its_guest(cmds, verify, services)" in src
    assert src.index("send_loopback_into_its_guest") < src.index("loopback_on_the_host(cmds")
