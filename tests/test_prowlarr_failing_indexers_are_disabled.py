r"""§17.1524 — "disable the failing Prowlarr indexers", done deterministically.

Live (job aefaef87, T5 "Disable failing indexers"): the drafter's frame was an unfilled template —
`PUT /api/v1/indexer/<INDEXER_ID>` with body `{"enable": false, "id": <INDEXER_ID>, "name":
"<INDEXER_NAME>"}`. The id/name are never filled (inert), and even filled it is wrong twice: an
*arr PUT validates the WHOLE object (a three-field body is rejected on its missing required
fields), and saving runs the indexer's connection test — which a FAILING indexer cannot pass, so
the save is refused without `?forceSave=true`. The engine PUTs each failing indexer's OWN full
object back with enable=false and forceSave=true, inside the guest, python3 (no jq).
"""
from __future__ import annotations

import io
import json
import subprocess
import contextlib
import unittest.mock as mock
import urllib.request as ur

from app.modules.supervised_runs import (
    rewrite_disable_failing_indexers, _PROWLARR_DISABLE_FAILING_PY,
)
from app.modules import machine_values as mv

SERVICES = [type("S", (), {"name": "prowlarr", "guest": "102", "ports": ("9696",)})()]
INV = {"names": {"102": "prowlarr"}}
NODE = {"title": "Disable failing indexers",
        "prompt_template": "Disable the Prowlarr indexers that are failing so they stop causing errors."}


def test_the_step_is_rewritten_to_the_indexer_api_in_the_guest():
    cmds, verify, notes = rewrite_disable_failing_indexers(NODE, ["<model>"], ["chk"], SERVICES, INV)
    assert notes and "§17.1524" in notes[0]["why"]
    assert len(cmds) == 1 and cmds[0].startswith("pct exec 102 -- sh -c '")
    assert "/api/v1/indexer" in cmds[0] and "<PROWLARR_API_KEY>" in cmds[0] and "python3" in cmds[0]


def test_the_key_fills_and_both_commands_parse():
    cmds, verify, _ = rewrite_disable_failing_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    c, v, _ = mv.read_on_the_machine(cmds, verify, INV)
    assert "<PROWLARR_API_KEY>" not in c[0] and "/var/lib/prowlarr/config.xml" in c[0]
    assert subprocess.run(["bash", "-n", "-c", c[0]], capture_output=True, text=True).returncode == 0
    assert subprocess.run(["bash", "-n", "-c", v[0]], capture_output=True, text=True).returncode == 0


def test_the_verify_is_read_only_safe_and_in_the_guest():
    from app.modules.assist_state_check import read_only_command
    _, verify, _ = rewrite_disable_failing_indexers(NODE, ["x"], ["true"], SERVICES, INV)
    v = verify[0]
    assert v != "true" and v.startswith("pct exec 102 -- curl") and "9696" in v
    assert "python3" not in v and "$(" not in v and read_only_command(v)


def _run_stub(indexers, status):
    calls = []

    def fake(req, timeout=None):
        calls.append((req.get_method(), req.full_url, json.loads(req.data) if req.data else None))
        if req.full_url.endswith("/api/v1/indexer"):
            return io.BytesIO(json.dumps(indexers).encode())
        if req.full_url.endswith("/api/v1/indexerstatus"):
            return io.BytesIO(json.dumps(status).encode())
        return io.BytesIO(b"")  # the PUT responses

    with mock.patch.object(ur, "urlopen", fake):
        import sys
        sys.argv = ["-c", "thekey"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(_PROWLARR_DISABLE_FAILING_PY, {"__name__": "__main__"})
        return buf.getvalue(), calls


def test_only_failing_enabled_indexers_are_put_back_disabled_with_forcesave():
    indexers = [
        {"id": 1, "name": "Healthy", "enable": True, "protocol": "torrent", "privacy": "public",
         "fields": [{"name": "baseUrl", "value": "http://a"}], "implementation": "Cardigann"},
        {"id": 2, "name": "BrokenA", "enable": True, "protocol": "torrent", "privacy": "public",
         "fields": [{"name": "baseUrl", "value": "http://b"}], "implementation": "Cardigann"},
        {"id": 3, "name": "BrokenB", "enable": True, "protocol": "torrent", "privacy": "public",
         "fields": [], "implementation": "Cardigann"},
        {"id": 4, "name": "AlreadyOff", "enable": False, "protocol": "torrent", "privacy": "public"},
    ]
    status = [{"indexerId": 2}, {"indexerId": 3}, {"indexerId": 4}]  # 4 is failing but already disabled
    out, calls = _run_stub(indexers, status)
    puts = [c for c in calls if c[0] == "PUT"]
    assert len(puts) == 2, "only the two failing-AND-enabled indexers are disabled"
    ids_put = sorted(int(c[1].rstrip("/").split("/")[-1].split("?")[0]) for c in puts)
    assert ids_put == [2, 3]
    # forceSave on every PUT, and the FULL object (enable flipped, other fields intact)
    for _m, url, body in puts:
        assert "forceSave=true" in url
        assert body["enable"] is False and "implementation" in body and "protocol" in body
    assert "FAILING+ENABLED: 2 of 4" in out and "RESULT: disabled 2" in out
    assert "BrokenA" in out and "BrokenB" in out and "Healthy" not in out.split("RESULT")[0].split("DISABLED")[-1]


def test_a_healthy_library_disables_nothing():
    indexers = [{"id": 1, "name": "Fine", "enable": True, "protocol": "torrent", "privacy": "public"}]
    out, calls = _run_stub(indexers, [])
    assert "FAILING+ENABLED: 0 of 1" in out and "disabled 0 failing" in out
    assert not [c for c in calls if c[0] == "PUT"], "nothing failing → no writes"


def test_the_deploy_does_not_trip_the_host_tool_gate():
    from app.modules.supervised_runs import host_only_inside_a_guest
    cmds, verify, _ = rewrite_disable_failing_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    assert not host_only_inside_a_guest(cmds + verify)


def test_the_loop_is_bounded_so_the_budget_gate_is_clean():
    # §17.1525 — the live frame was refused: the loop PUT once per item, which the
    # network-loop gate treats as open-ended (one command = 180 s). The loop is bound to a
    # slice with a short per-request timeout so a batch finishes well inside the budget.
    from app.modules.supervised_runs import loops_the_network_without_a_budget
    cmds, _, _ = rewrite_disable_failing_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    assert "targets[:15]" in cmds[0] and "timeout=10" in cmds[0]
    assert loops_the_network_without_a_budget(cmds) == [], "the bounded batch must clear the gate"


def test_non_disable_and_add_remove_steps_are_left_alone():
    for t in ("Inventory Prowlarr indexers", "Add general TV indexers", "Remove Cloudflare indexers",
              "Sync indexers to Sonarr", "Identify failing indexers"):
        out, _, notes = rewrite_disable_failing_indexers({"title": t}, ["x"], ["y"], SERVICES, INV)
        assert out == ["x"] and not notes, t


def test_no_prowlarr_guest_leaves_the_draft_untouched():
    out, _, notes = rewrite_disable_failing_indexers(NODE, ["x"], ["y"], [], {"names": {}})
    assert out == ["x"] and not notes


def test_frame_run_runs_the_disable_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_disable_failing_indexers(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_disable_failing_indexers(node, cmds") < src.index("read_on_the_machine(cmds")
