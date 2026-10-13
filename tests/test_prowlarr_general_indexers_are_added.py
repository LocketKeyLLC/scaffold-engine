r"""§17.1526 — "add general-purpose public TV indexers", done deterministically (ASK7: public,
no-login only).

Live (job aefaef87, T7 "Add general TV indexers"): the model drafted a 24-command kitchen sink
re-doing the whole job (setup_flaresolverr.sh, disable_failing ×5, remove_stalled ×16, …) with one
line actually about T7, and the frame was refused. The engine reads the Prowlarr definition catalog
(/api/v1/indexer/schema) and current indexers, and for each name in a curated set of general/TV
PUBLIC trackers not already present, POSTs the schema's own template with forceSave=true (a
Cloudflare-gated one is tested later through FlareSolverr). python3 in-guest (no jq), bounded.
"""
from __future__ import annotations

import io
import json
import subprocess
import contextlib
import unittest.mock as mock
import urllib.request as ur
import urllib.error as uerr

from app.modules.supervised_runs import (
    rewrite_add_general_tv_indexers, _PROWLARR_ADD_INDEXERS_PY, _PROWLARR_TV_INDEXERS,
)
from app.modules import machine_values as mv

SERVICES = [type("S", (), {"name": "prowlarr", "guest": "102", "ports": ("9696",)})()]
INV = {"names": {"102": "prowlarr"}}
NODE = {"title": "Add general TV indexers",
        "prompt_template": "Add general-purpose public indexers (no login) to Prowlarr for more TV sources."}


def test_the_step_is_rewritten_to_the_schema_and_indexer_api_in_the_guest():
    cmds, verify, notes = rewrite_add_general_tv_indexers(NODE, ["<model>"], ["chk"], SERVICES, INV)
    assert notes and "§17.1526" in notes[0]["why"]
    assert len(cmds) == 1 and cmds[0].startswith("pct exec 102 -- sh -c '")
    assert "/api/v1/indexer/schema" in cmds[0] and "<PROWLARR_API_KEY>" in cmds[0] and "python3" in cmds[0]


def test_the_key_fills_and_both_commands_parse():
    cmds, verify, _ = rewrite_add_general_tv_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    c, v, _ = mv.read_on_the_machine(cmds, verify, INV)
    assert "<PROWLARR_API_KEY>" not in c[0] and "/var/lib/prowlarr/config.xml" in c[0]
    assert subprocess.run(["bash", "-n", "-c", c[0]], capture_output=True, text=True).returncode == 0
    assert subprocess.run(["bash", "-n", "-c", v[0]], capture_output=True, text=True).returncode == 0


def test_the_verify_is_read_only_safe_and_in_the_guest():
    from app.modules.assist_state_check import read_only_command
    _, verify, _ = rewrite_add_general_tv_indexers(NODE, ["x"], ["true"], SERVICES, INV)
    v = verify[0]
    assert v != "true" and v.startswith("pct exec 102 -- curl") and "9696" in v
    assert "python3" not in v and "$(" not in v and read_only_command(v)


def test_the_loop_is_bounded_so_the_budget_gate_is_clean():
    from app.modules.supervised_runs import loops_the_network_without_a_budget
    cmds, _, _ = rewrite_add_general_tv_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    assert "WANT[:12]" in cmds[0] and "timeout=12" in cmds[0]
    assert loops_the_network_without_a_budget(cmds) == []


def test_a_hang_is_handled_inside_the_loop():
    # §17.1527 — a tracker that does not answer raises URLError/TimeoutError/OSError (not
    # HTTPError); api() must catch those so the batch is not killed mid-loop with no record.
    cmds, _, _ = rewrite_add_general_tv_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    assert "urllib.error.URLError, TimeoutError, OSError" in cmds[0]

    # exercise it: a POST that raises TimeoutError is recorded, not fatal
    schema = [{"name": "1337x", "fields": [], "implementation": "Cardigann"},
              {"name": "EZTV", "fields": [], "implementation": "Cardigann"}]
    calls = {"n": 0}

    def fake(req, timeout=None):
        if req.full_url.endswith("/api/v1/indexer/schema"):
            return io.BytesIO(json.dumps(schema).encode())
        if req.full_url.endswith("/api/v1/indexer") and req.get_method() == "GET":
            return io.BytesIO(json.dumps([]).encode())
        if req.get_method() == "POST":
            calls["n"] += 1
            if calls["n"] == 1:
                raise TimeoutError("timed out")
            return io.BytesIO(json.dumps({"id": 5}).encode())
        return io.BytesIO(b"")

    with mock.patch.object(ur, "urlopen", fake):
        import sys
        sys.argv = ["-c", "k"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(_PROWLARR_ADD_INDEXERS_PY, {"__name__": "__main__"})
    out = buf.getvalue()
    assert "FAILED: 1337x: unreachable" in out and "ADDED: 1 - EZTV" in out


def test_the_curated_set_is_public_tv_general_and_excludes_whats_already_there():
    # ASK7 is public/no-login; the inventory already had Nyaa/TPB/YTS etc. The set targets the
    # general/TV gap and must not re-list ones the measured inventory already carried.
    names = {n.lower() for n in _PROWLARR_TV_INDEXERS}
    assert "1337x" in names and "eztv" in names and "torrentgalaxy" in names
    already_present = {"nyaa.si", "the pirate bay", "yts", "knaben", "torrentscsv"}
    assert not (names & already_present), "do not re-add indexers already in the measured inventory"


def _run(schema, existing, put_errors=None):
    """put_errors: dict name.lower() -> HTTPError body string to simulate a rejected POST."""
    put_errors = put_errors or {}
    posts = []

    def fake(req, timeout=None):
        url = req.full_url
        if url.endswith("/api/v1/indexer/schema"):
            return io.BytesIO(json.dumps(schema).encode())
        if url.endswith("/api/v1/indexer") and req.get_method() == "GET":
            return io.BytesIO(json.dumps(existing).encode())
        if "indexer" in url and req.get_method() == "POST":
            body = json.loads(req.data)
            posts.append((url, body))
            key = (body.get("name") or "").lower()
            if key in put_errors:
                raise uerr.HTTPError(url, 400, "Bad Request", {}, io.BytesIO(put_errors[key].encode()))
            return io.BytesIO(json.dumps({"id": 99, "name": body.get("name")}).encode())
        return io.BytesIO(b"")

    with mock.patch.object(ur, "urlopen", fake):
        import sys
        sys.argv = ["-c", "thekey"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(_PROWLARR_ADD_INDEXERS_PY, {"__name__": "__main__"})
        return buf.getvalue(), posts


def test_only_wanted_absent_schema_backed_indexers_are_posted_with_forcesave():
    schema = [
        {"name": "1337x", "fields": [{"name": "baseUrl", "value": "http://1337x"}], "implementation": "Cardigann"},
        {"name": "EZTV", "fields": [], "implementation": "Cardigann"},
        {"name": "The Pirate Bay", "fields": [], "implementation": "Cardigann"},
        {"name": "SomethingElse", "fields": [], "implementation": "Cardigann"},
    ]
    existing = [{"name": "The Pirate Bay"}, {"name": "Nyaa.si"}]  # TPB already present
    out, posts = _run(schema, existing)
    posted_names = {b["name"] for _u, b in posts}
    assert posted_names == {"1337x", "EZTV"}, "post only the wanted ones present in schema and not already configured"
    for u, b in posts:
        assert "forceSave=true" in u and b["enable"] is True and b.get("appProfileId", 0) > 0
    assert "ADDED: 2 - 1337x, EZTV" in out
    assert "already present" not in out.split("SKIPPED")[0]  # TPB wasn't in WANT, so not even a skip line for it
    assert "RESULT: added 2" in out


def test_a_rejected_post_is_reported_not_fatal():
    schema = [
        {"name": "1337x", "fields": [], "implementation": "Cardigann"},
        {"name": "EZTV", "fields": [], "implementation": "Cardigann"},
    ]
    out, posts = _run(schema, [], put_errors={"1337x": '[{"propertyName":"","errorMessage":"Unable to connect, blocked by CloudFlare Protection"}]'})
    assert "FAILED: 1337x:" in out and "CloudFlare" in out
    assert "ADDED: 1 - EZTV" in out and "RESULT: added 1" in out


def test_a_name_absent_from_the_schema_is_skipped_not_failed():
    out, posts = _run([{"name": "1337x", "fields": [], "implementation": "Cardigann"}], [])
    assert "not in schema" in out  # the other WANT names have no schema entry
    assert "ADDED: 1 - 1337x" in out and not any("FAILED" in l for l in out.splitlines())


def test_the_add_does_not_trip_the_host_tool_gate():
    from app.modules.supervised_runs import host_only_inside_a_guest
    cmds, verify, _ = rewrite_add_general_tv_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    assert not host_only_inside_a_guest(cmds + verify)


def test_non_add_steps_are_left_alone():
    for t in ("Inventory Prowlarr indexers", "Disable failing indexers", "Remove Cloudflare indexers",
              "Sync indexers to Sonarr", "Identify failing indexers"):
        out, _, notes = rewrite_add_general_tv_indexers({"title": t}, ["x"], ["y"], SERVICES, INV)
        assert out == ["x"] and not notes, t


def test_no_prowlarr_guest_leaves_the_draft_untouched():
    out, _, notes = rewrite_add_general_tv_indexers(NODE, ["x"], ["y"], [], {"names": {}})
    assert out == ["x"] and not notes


def test_frame_run_runs_the_add_before_the_key_reader():
    import inspect
    from app.modules import supervised_runs as sr
    src = inspect.getsource(sr)
    assert "rewrite_add_general_tv_indexers(node, cmds, verify, services, inventory)" in src
    assert src.index("rewrite_add_general_tv_indexers(node, cmds") < src.index("read_on_the_machine(cmds")
