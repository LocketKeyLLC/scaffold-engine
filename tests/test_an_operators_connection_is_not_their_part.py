"""§17.1487 — "the operator's own connection" is a possession, not the operator's part of a step.

Live (2026-10-11): ADD159 "CT 105 (download-client): stop qBittorrent until it has a VPN" (fixture, the original
text) — one `systemctl stop` the engine can run — was parked as the operator's to do by hand, because its measured
fact said qBittorrent "sends its traffic on the operator's own connection".
"""
import json
import pathlib

from app.modules import operator_steps as ops

NODE = json.loads(pathlib.Path("tests/fixtures/add159_original_2026_10_11.json").read_text())


def test_the_live_step_is_the_engines():
    assert ops.operators_part(NODE) == "" and ops.is_wholly_operators(NODE) is False


def test_a_place_the_operator_works_still_is_theirs():
    for t in ("On the operator's own phone, open the Tailscale app and sign in.",
              "This is the operator's part: approve the route in the admin console.",
              "Log in on the operator's own screen and confirm."):
        assert ops.operators_part({"tool": "Shell", "description": t}), t


def test_a_stopped_service_is_the_stop_step_succeeding():
    """§17.1487 — ADD159 was failed "systemctl is-active says 'inactive'" — the one answer that proved it worked."""
    from app.modules.assist_state_check import negative_evidence as ne
    cmd = 'pct exec 105 -- bash -c "systemctl is-active qbittorrent-nox && systemctl is-enabled qbittorrent-nox"'
    claim = "CT 105 (download-client): stop qBittorrent until it has a VPN"
    assert ne(cmd, "inactive", claim) == ""
    assert ne(cmd, "inactive", "Start Caddy in CT 120") == "systemctl is-active says 'inactive'"
    assert ne("ls /etc/wireguard", "ls: cannot access '/etc/wireguard': No such file or directory",
              "Remove the old WireGuard config") == ""


def test_waiting_for_something_is_not_undoing_it():
    from app.modules.assist_state_check import negative_evidence as ne
    assert ne("pct exec 120 -- systemctl is-active caddy", "inactive",
              "Wait until it has an address, then start Caddy") == "systemctl is-active says 'inactive'"


def test_what_the_operator_reports_is_not_their_part():
    """§17.1493 — the only two uses across every plan state what the operator said (ADD169's handed it to them)."""
    for t in ("The operator reports the torrents look halted WITH several peers, so also print the states.",
              "The operator reports both saved; read the router back to confirm."):
        assert ops.operators_part({"tool": "Shell", "description": t}) == "", t
    assert ops.operators_part({"tool": "Shell", "description": "Then the operator approves the route in the admin console."})
