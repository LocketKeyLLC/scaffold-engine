r"""§17.1563 — a service that times out on a public API because its guest prefers a dead IPv6 route gets
IPv4 preferred (gai.conf precedence) and its unit restarted.

Live (CT 101, 2026-10-14): `getent ahosts api.themoviedb.org` listed eight 2600:… addresses first; `curl -6` →
nothing, `curl -4` → 401 (reachable); Jellyfin's log carried 67 TMDb `HttpClient.Timeout of 100 seconds` traces
that day, so no overview or poster could land however the library was laid out. §17.1532/1534 fixed the same
class for Prowlarr inline; this is the capability.
"""
from __future__ import annotations

import re
import subprocess

from app.modules.supervised_runs import (rewrite_prefer_ipv4_in_guest, checks_can_witness_the_change,
                                         rewrite_refresh_jellyfin_metadata, rewrite_rebuild_jellyfin_library)


class _Svc:
    def __init__(self, name, guest, ports):
        self.name, self.guest, self.ports = name, guest, ports


SERVICES = [_Svc("jellyfin", "101", ("8096",)), _Svc("prowlarr", "103", ("9696",))]
INV = {"names": {"101": "jellyfin", "103": "prowlarr", "104": "sonarr"}}
FIX2 = {"node_key": "FIX2", "title": "Make Jellyfin on container 101 reach TMDb",
        "prompt_template": "Jellyfin's HTTP client prefers IPv6 and the guest's IPv6 has no route, so every "
                           "api.themoviedb.org call times out after 100 seconds (67 traces today). Prefer IPv4 in the "
                           "guest and restart jellyfin.service.", "tool": "Shell"}


def test_the_step_is_rewritten_to_gai_precedence_plus_a_unit_restart_in_the_guest():
    cmds, verify, notes = rewrite_prefer_ipv4_in_guest(FIX2, ["<model>"], ["chk"], SERVICES, INV)
    assert notes and "§17.1563" in notes[0]["why"] and "api.themoviedb.org" in notes[0]["why"]
    assert len(cmds) == 1 and cmds[0].startswith("pct exec 101 -- sh -c '")
    assert "precedence ::ffff:0:0/96  100" in cmds[0] and "/etc/gai.conf" in cmds[0]
    assert "systemctl restart jellyfin.service" in cmds[0] and "is-active jellyfin.service" in cmds[0]
    assert "grep -qxF" in cmds[0]                                 # written once: idempotent
    assert subprocess.run(["bash", "-n", "-c", cmds[0]], capture_output=True, text=True).returncode == 0


def test_the_verify_reads_the_change_back_and_is_read_only():
    from app.modules.assist_state_check import read_only_command
    cmds, verify, _ = rewrite_prefer_ipv4_in_guest(FIX2, ["x"], ["y"], SERVICES, INV)
    v = verify[0]
    assert 'grep -c "precedence ::ffff:0:0/96  100" /etc/gai.conf' in v     # the line, read back
    assert "getent ahosts api.themoviedb.org | head -n 1" in v              # which family wins now
    assert 'curl -s -m 8 -o /dev/null -w "api.themoviedb.org HTTP %{http_code}" https://api.themoviedb.org/' in v
    assert read_only_command(v)
    assert subprocess.run(["bash", "-n", "-c", v], capture_output=True, text=True).returncode == 0
    # not a liveness probe and not an HTTP trigger: the pre-pass may read it, and before the fix the
    # grep count is 0 and getent orders an IPv6 first — a reading that can FAIL (§17.1365's spirit)
    assert checks_can_witness_the_change(cmds, verify) == ""


def test_the_app_and_guest_come_from_the_step_and_the_probe_host_from_its_text():
    node = {"title": "Prowlarr cannot reach prowlarr.servarr.com", "tool": "Shell",
            "description": "Prowlarr times out on IPv6 when fetching indexer definitions; prefer IPv4 in container 103."}
    cmds, verify, notes = rewrite_prefer_ipv4_in_guest(node, ["x"], ["y"], SERVICES, INV)
    assert notes and cmds[0].startswith("pct exec 103 -- ") and "restart prowlarr.service" in cmds[0]
    assert "prowlarr.servarr.com" in verify[0]
    unknown = {"title": "Make Lidarr reach its metadata provider", "description": "it times out on IPv6", "tool": "Shell"}
    assert not rewrite_prefer_ipv4_in_guest(unknown, ["x"], ["y"], SERVICES, INV)[2]      # no lidarr guest known


def test_a_guest_that_is_not_an_arr_app_is_resolved_from_the_measured_service_or_the_inventory():
    """§17.1563b — live, the same dead IPv6 route hits 111 control-panel, 120 caddy and 130 pihole (unit
    pihole-FTL.service): none is in the *arr list, so the guest and unit come from what was measured."""
    class _Unit(_Svc):
        def __init__(self, name, guest, ports, unit):
            super().__init__(name, guest, ports); self.unit = unit
    svcs = [_Unit("pihole", "130", ("53",), "pihole-FTL.service"), _Unit("caddy", "120", ("443",), "caddy.service")]
    inv = {"names": {"120": "caddy-proxy", "130": "pihole", "111": "control-panel"}}
    node = {"title": "Make Pi-hole on container 130 reach its gravity lists", "tool": "Shell",
            "description": "pihole times out on IPv6 fetching raw.githubusercontent.com; prefer IPv4 in the guest."}
    cmds, verify, notes = rewrite_prefer_ipv4_in_guest(node, ["x"], ["y"], svcs, inv)
    assert notes and cmds[0].startswith("pct exec 130 -- ") and "restart pihole-FTL.service" in cmds[0]
    assert "raw.githubusercontent.com" in verify[0]
    # by inventory name when nothing measured names it, with the unit from a measured service on that guest
    node2 = {"title": "Caddy-proxy cannot reach Let's Encrypt over IPv6", "tool": "Shell",
             "description": "prefer IPv4 for acme-v02.api.letsencrypt.org on the caddy-proxy guest"}
    cmds2, _, notes2 = rewrite_prefer_ipv4_in_guest(node2, ["x"], ["y"], svcs, inv)
    assert notes2 and cmds2[0].startswith("pct exec 120 -- ") and "restart caddy.service" in cmds2[0]
    # a guest with NO measured unit is left alone (nothing safe to restart)
    node3 = {"title": "Control-panel on container 111 times out on IPv6", "tool": "Shell", "description": "prefer IPv4"}
    assert not rewrite_prefer_ipv4_in_guest(node3, ["x"], ["y"], svcs, inv)[2]


def test_unrelated_steps_are_left_to_their_own_capabilities_or_the_model():
    for node in ({"title": "Trigger metadata refresh", "prompt_template": "POST refresh with ReplaceAllMetadata=true for each Jellyfin library."},
                 {"title": "Fix the Jellyfin TV library so its files resolve as Series", "prompt_template": "the library resolved its items as Movie"},
                 {"title": "Set onboot on VM 110", "description": "qm set 110 --onboot 1"},
                 {"title": "Verify Jellyfin answers", "description": "curl http://127.0.0.1:8096/System/Info/Public times out? report it"}):
        assert not rewrite_prefer_ipv4_in_guest(node, ["x"], ["y"], SERVICES, INV)[2], node["title"]
    assert not rewrite_refresh_jellyfin_metadata(FIX2, ["x"], ["y"], SERVICES, INV)[2]
    assert not rewrite_rebuild_jellyfin_library(FIX2, ["x"], ["y"], SERVICES, INV)[2]


def test_frame_run_runs_the_prefer_ipv4_rewrite_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_prefer_ipv4_in_guest(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_prefer_ipv4_in_guest(node, cmds") < src.index("read_on_the_machine(cmds")
