r"""§17.1523 — "inventory the Prowlarr indexers", done deterministically.

Live (job aefaef87, T2 "Inventory Prowlarr indexers"): the drafter wrapped the read in the
§17.1317 detached template but the in-guest script `systemctl stop prowlarr.service` BEFORE
curling its own API (it copied the machine-truth note "the service rewrites config.xml, stop it
before editing" onto a read-only inventory — so the API it then curls is dead), parsed with `jq`,
and the verify ran on the HOST against loopback 9696 (the host does not listen there → §17.1368
refused the frame). The engine does this exactly: inside the Prowlarr guest, WITHOUT touching the
service, GET /api/v1/indexer + /api/v1/indexerstatus and print the table (python3, no jq); the
verify is a read-only authenticated read of the same endpoint.
"""
from __future__ import annotations

import io
import json
import subprocess
import contextlib
import unittest.mock as mock
import urllib.request as ur

from app.modules.supervised_runs import (
    rewrite_inventory_prowlarr_indexers, _PROWLARR_INVENTORY_PY,
)
from app.modules import machine_values as mv

SERVICES = [type("S", (), {"name": "prowlarr", "guest": "102", "ports": ("9696",)})()]
INV = {"names": {"102": "prowlarr"}}
NODE = {"title": "Inventory Prowlarr indexers",
        "prompt_template": "List every indexer configured in Prowlarr with its name, status and protocol."}


def test_the_step_is_rewritten_to_the_indexer_api_in_the_guest():
    cmds, verify, notes = rewrite_inventory_prowlarr_indexers(NODE, ["<model>"], ["chk"], SERVICES, INV)
    assert notes and "§17.1523" in notes[0]["why"]
    assert len(cmds) == 1 and cmds[0].startswith("pct exec 102 -- sh -c '")
    assert "/api/v1/indexer" in cmds[0] and "<PROWLARR_API_KEY>" in cmds[0]


def test_the_read_never_stops_the_service_and_uses_no_jq():
    # the live mis-draft stopped prowlarr before reading it and shelled out to jq.
    cmds, _, _ = rewrite_inventory_prowlarr_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    assert "systemctl stop" not in cmds[0] and "jq" not in cmds[0]
    assert "python3" in cmds[0]


def test_the_key_fills_and_both_commands_parse():
    cmds, verify, _ = rewrite_inventory_prowlarr_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    c, v, _ = mv.read_on_the_machine(cmds, verify, INV)
    assert "<PROWLARR_API_KEY>" not in c[0] and "/var/lib/prowlarr/config.xml" in c[0]
    assert subprocess.run(["bash", "-n", "-c", c[0]], capture_output=True, text=True).returncode == 0
    assert subprocess.run(["bash", "-n", "-c", v[0]], capture_output=True, text=True).returncode == 0


def test_the_verify_is_read_only_safe_and_in_the_guest():
    from app.modules.assist_state_check import read_only_command
    _, verify, _ = rewrite_inventory_prowlarr_indexers(NODE, ["x"], ["true"], SERVICES, INV)
    v = verify[0]
    assert v != "true" and v.startswith("pct exec 102 -- curl") and "9696" in v
    assert "python3" not in v and "$(" not in v and read_only_command(v)


def test_the_verify_does_not_run_on_the_host():
    # §17.1368 — a host-side curl to the guest's loopback port is refused; the verify must
    # reach 9696 THROUGH the guest, not from the host.
    from app.modules.supervised_runs import host_only_inside_a_guest
    cmds, verify, _ = rewrite_inventory_prowlarr_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    assert not host_only_inside_a_guest(cmds + verify)


def _run_stub(indexers, status):
    calls = []

    def fake(req, timeout=None):
        calls.append(req.full_url)
        if req.full_url.endswith("/api/v1/indexer"):
            return io.BytesIO(json.dumps(indexers).encode())
        if req.full_url.endswith("/api/v1/indexerstatus"):
            return io.BytesIO(json.dumps(status).encode())
        return io.BytesIO(b"")

    with mock.patch.object(ur, "urlopen", fake):
        import sys
        sys.argv = ["-c", "thekey"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(_PROWLARR_INVENTORY_PY, {"__name__": "__main__"})
        return buf.getvalue(), calls


def test_the_table_and_summary_name_the_failing_indexers():
    indexers = [
        {"id": 1, "name": "1337x", "enable": True, "protocol": "torrent", "privacy": "public"},
        {"id": 2, "name": "The Pirate Bay", "enable": True, "protocol": "torrent", "privacy": "public"},
        {"id": 3, "name": "BrokenTracker", "enable": True, "protocol": "torrent", "privacy": "public"},
        {"id": 4, "name": "DisabledOne", "enable": False, "protocol": "torrent", "privacy": "public"},
    ]
    status = [{"indexerId": 3, "disabledTill": "2099-01-01T00:00:00Z"}]
    out, calls = _run_stub(indexers, status)
    assert "INDEXERS: 4 configured" in out
    assert "SUMMARY: 4 indexers, 3 enabled, 1 failing: BrokenTracker" in out
    # the failing one is marked, the healthy ones are not
    lines = {l.split("\t")[1]: l for l in out.splitlines() if "\t" in l and not l.startswith("id\t")}
    assert lines["BrokenTracker"].endswith("FAILING") and lines["1337x"].endswith("ok")
    assert any(u.endswith("/api/v1/indexerstatus") for u in calls)


def test_a_missing_indexerstatus_does_not_crash_the_inventory():
    # a fresh Prowlarr has no indexerstatus rows; the read must still print the table.
    def fake(req, timeout=None):
        if req.full_url.endswith("/api/v1/indexer"):
            return io.BytesIO(json.dumps(
                [{"id": 1, "name": "Nyaa", "enable": True, "protocol": "torrent", "privacy": "public"}]).encode())
        raise ur.HTTPError(req.full_url, 404, "no status", {}, None)

    with mock.patch.object(ur, "urlopen", fake):
        import sys
        sys.argv = ["-c", "k"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(_PROWLARR_INVENTORY_PY, {"__name__": "__main__"})
    out = buf.getvalue()
    assert "INDEXERS: 1 configured" in out and "0 failing" in out and "Nyaa" in out


def test_identify_failing_step_reuses_the_same_inventory():
    # T3 "Identify failing indexers" is a read of the same data — the FAILING column answers it.
    node = {"title": "Identify failing indexers", "prompt_template": "Find the indexers that are failing."}
    _, _, notes = rewrite_inventory_prowlarr_indexers(node, ["x"], ["y"], SERVICES, INV)
    assert notes


def test_write_and_cloudflare_steps_are_left_alone():
    # T5 disable, T6 remove, T7 add, T9 sync, T4 cloudflare — each keeps its own path, not this read.
    for t in ("Disable failing indexers", "Remove Cloudflare indexers", "Add general TV indexers",
              "Sync indexers to Sonarr", "Identify Cloudflare-protected indexers"):
        out, _, notes = rewrite_inventory_prowlarr_indexers({"title": t}, ["x"], ["y"], SERVICES, INV)
        assert out == ["x"] and not notes, t


def test_no_prowlarr_guest_leaves_the_draft_untouched():
    out, _, notes = rewrite_inventory_prowlarr_indexers(NODE, ["x"], ["y"], [], {"names": {}})
    assert out == ["x"] and not notes


def test_frame_run_runs_the_inventory_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_inventory_prowlarr_indexers(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_inventory_prowlarr_indexers(node, cmds") < src.index("read_on_the_machine(cmds")
