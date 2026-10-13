r"""§17.1526/1528 — "add general-purpose public TV indexers", routing the Cloudflare-gated ones
through FlareSolverr (ASK7: public/no-login only; T1: keep Cloudflare indexers via FlareSolverr).

Live (job aefaef87, T7 "Add general TV indexers"): the model drafted a 24-command kitchen sink
re-doing the whole job, and was refused. The first deterministic draft (§1526) then taught two live
facts: the curated names mostly did not exist in the 659-definition catalog (the real general/TV
public names are the measured set below), and forceSave does NOT skip the connection test on an ADD,
so a Cloudflare-gated tracker (1337x, EZTV) cannot be added until its requests route through
FlareSolverr. §1528 ensures a flaresolverr tag + indexer-proxy, then POSTs each wanted tracker and,
on a Cloudflare refusal, retries the POST WITH the flaresolverr tag so the test runs through the
proxy. python3 in-guest (no jq), bounded, hang-safe.
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
    rewrite_add_general_tv_indexers, _PROWLARR_ADD_INDEXERS_PY, _PROWLARR_TV_INDEXERS, _FS_IP,
)
from app.modules import machine_values as mv

SERVICES = [type("S", (), {"name": "prowlarr", "guest": "102", "ports": ("9696",)})()]
INV = {"names": {"102": "prowlarr"}}
NODE = {"title": "Add general TV indexers",
        "prompt_template": "Add general-purpose public indexers (no login) to Prowlarr for more TV sources."}


def test_the_step_is_rewritten_to_the_schema_and_indexer_api_in_the_guest():
    cmds, verify, notes = rewrite_add_general_tv_indexers(NODE, ["<model>"], ["chk"], SERVICES, INV)
    assert notes and ("§17.1526" in notes[0]["why"] or "§17.1528" in notes[0]["why"])
    # cmd[0] prepares IPv4 preference; cmd[1] is the API add.
    assert len(cmds) == 2
    add = cmds[-1]
    assert add.startswith("pct exec 102 -- sh -c '")
    assert "/api/v1/indexer/schema" in add and "<PROWLARR_API_KEY>" in add and "python3" in add
    assert "/api/v1/indexerproxy" in add and "/api/v1/tag" in add


def test_the_first_command_makes_prowlarr_prefer_ipv4_then_restarts(monkeypatch):
    # §17.1532 — FlareSolverr clears CF over IPv4; Prowlarr must stop preferring the tracker's IPv6
    # (Prowlarr #2810). cmd[0] adds a gai.conf IPv4 precedence, best-effort disables IPv6, and
    # restarts prowlarr so it re-resolves — before the add runs.
    cmds, _, notes = rewrite_add_general_tv_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    ipv4 = cmds[0]
    assert ipv4.startswith("pct exec 102 -- sh -c '")
    assert "precedence ::ffff:0:0/96  100" in ipv4 and "/etc/gai.conf" in ipv4
    assert "disable_ipv6=1" in ipv4 and "systemctl restart prowlarr" in ipv4
    assert "n=0; while [ $n -lt 25 ]" in ipv4 and "$SECONDS" not in ipv4
    assert "1532" in notes[0]["why"] and "IPv4" in notes[0]["why"]


def test_the_key_fills_and_both_commands_parse():
    cmds, verify, _ = rewrite_add_general_tv_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    c, v, _ = mv.read_on_the_machine(cmds, verify, INV)
    # the API-add command (last) carries the key and gets it filled on the machine
    assert "<PROWLARR_API_KEY>" not in c[-1] and "/var/lib/prowlarr/config.xml" in c[-1]
    for cmd in c + v:   # every command (IPv4 prep, add, verify) is valid bash
        assert subprocess.run(["bash", "-n", "-c", cmd], capture_output=True, text=True).returncode == 0


def test_the_verify_is_read_only_safe_and_in_the_guest():
    from app.modules.assist_state_check import read_only_command
    _, verify, _ = rewrite_add_general_tv_indexers(NODE, ["x"], ["true"], SERVICES, INV)
    v = verify[0]
    assert v != "true" and v.startswith("pct exec 102 -- curl") and "9696" in v
    assert "python3" not in v and "$(" not in v and read_only_command(v)


def test_the_loop_is_bounded_and_hang_safe_so_the_gates_are_clean():
    from app.modules.supervised_runs import loops_the_network_without_a_budget
    cmds, _, _ = rewrite_add_general_tv_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    assert "WANT[:10]" in cmds[-1] and "timeout=12" in cmds[-1]
    assert "urllib.error.URLError, TimeoutError, OSError" in cmds[-1]
    assert loops_the_network_without_a_budget([cmds[-1]]) == []


def test_the_fs_proxy_host_points_at_the_deployed_flaresolverr():
    cmds, _, _ = rewrite_add_general_tv_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    assert ("http://" + _FS_IP + ":8191/") in cmds[-1]


def test_it_classifies_by_shape_not_by_a_phrase_list():
    # §17.1529 — the classify-by-phrase gate refuses `'cloudflare' in err`; the decision must come
    # from the error SHAPE (an empty propertyName = availability), never a word list.
    from app.modules.supervised_runs import classifies_by_key_presence
    cmds, _, _ = rewrite_add_general_tv_indexers(NODE, ["x"], ["y"], SERVICES, INV)
    assert "is_availability" in cmds[-1] and "propertyName" in cmds[-1]
    assert classifies_by_key_presence([cmds[-1]]) == [], "no phrase-list classification remains"


def test_the_curated_set_is_the_real_public_tv_names():
    names = {n.lower() for n in _PROWLARR_TV_INDEXERS}
    assert "1337x" in names and "eztv" in names and "limetorrents" in names
    # the ones the live catalog did NOT carry must not be reintroduced
    assert not ({"torrentgalaxy", "torlock", "glotorrents", "solid torrents", "therarbg"} & names)
    # and nothing already in the measured inventory
    assert not ({"nyaa.si", "the pirate bay", "yts", "knaben"} & names)


def _run(responses):
    """responses: dict mapping (METHOD, path-suffix) -> value or callable(body)->value.
    A value that is an Exception is raised; otherwise it is json-encoded as the response body."""
    seen = []

    def resolve(method, url, body):
        for (m, suffix), val in responses.items():
            if m == method and url.split("?")[0].endswith(suffix):
                return val(body) if callable(val) else val
        return {}

    def fake(req, timeout=None):
        method, url = req.get_method(), req.full_url
        body = json.loads(req.data) if req.data else None
        seen.append((method, url, body))
        val = resolve(method, url, body)
        if isinstance(val, Exception):
            raise val
        return io.BytesIO(json.dumps(val).encode())

    with mock.patch.object(ur, "urlopen", fake):
        import sys
        sys.argv = ["-c", "thekey"]
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            exec(_PROWLARR_ADD_INDEXERS_PY, {"__name__": "__main__"})
        return buf.getvalue(), seen


def _cf_error(host):
    return uerr.HTTPError("u", 400, "Bad Request", {}, io.BytesIO(
        json.dumps([{"propertyName": "", "errorMessage": "Unable to access %s, blocked by CloudFlare Protection." % host}]).encode()))


def test_the_tag_and_proxy_are_created_then_indexers_added_cf_via_flaresolverr():
    schema = [
        {"name": "1337x", "fields": [], "implementation": "Cardigann"},          # Cloudflare
        {"name": "TorrentProject2", "fields": [], "implementation": "Cardigann"},  # direct ok
    ]
    posted_indexers = []

    def post_indexer(body):
        # a POST carrying the flaresolverr tag succeeds; a direct 1337x POST is Cloudflare-blocked
        posted_indexers.append(body)
        if (body.get("name") == "1337x") and not body.get("tags"):
            return _cf_error("1337x.to")
        return {"id": 77, "name": body.get("name")}

    out, seen = _run({
        ("GET", "/api/v1/tag"): [],
        ("POST", "/api/v1/tag"): {"id": 3, "label": "flaresolverr"},
        ("GET", "/api/v1/indexerproxy"): [],
        ("GET", "/api/v1/indexerproxy/schema"): [
            {"implementation": "FlareSolverr", "name": "FlareSolverr",
             "fields": [{"name": "host", "value": "http://localhost:8191/"}]}],
        ("POST", "/api/v1/indexerproxy"): {"id": 1, "name": "FlareSolverr"},
        ("GET", "/api/v1/indexer/schema"): schema,
        ("GET", "/api/v1/indexer"): [],
        ("POST", "/api/v1/indexer"): post_indexer,
    })
    # proxy created with the deployed host and the flaresolverr tag
    proxy_post = next(b for (m, u, b) in seen if m == "POST" and u.split("?")[0].endswith("/api/v1/indexerproxy"))
    host_field = next(f for f in proxy_post["fields"] if f["name"] == "host")
    assert host_field["value"] == "http://%s:8191/" % _FS_IP and proxy_post["tags"] == [3]
    assert "PROXY: added FlareSolverr" in out
    # 1337x added only on the retry carrying the tag; TorrentProject2 added directly
    assert "ADDED (direct): 1 - TorrentProject2" in out
    assert "ADDED (via FlareSolverr): 1 - 1337x" in out
    assert "RESULT: added 2 public indexer(s) (1 direct, 1 via FlareSolverr)" in out
    tagged_1337x = [b for b in posted_indexers if b.get("name") == "1337x" and b.get("tags") == [3]]
    assert tagged_1337x and tagged_1337x[0]["enable"] is True


def test_an_existing_proxy_is_not_recreated():
    out, seen = _run({
        ("GET", "/api/v1/tag"): [{"id": 3, "label": "flaresolverr"}],
        ("GET", "/api/v1/indexerproxy"): [{"id": 1, "implementation": "FlareSolverr"}],
        ("GET", "/api/v1/indexer/schema"): [{"name": "EZTV", "fields": [], "implementation": "Cardigann"}],
        ("GET", "/api/v1/indexer"): [],
        ("POST", "/api/v1/indexer"): {"id": 5, "name": "EZTV"},
    })
    assert "PROXY: FlareSolverr already configured" in out
    assert not any(m == "POST" and u.split("?")[0].endswith("/api/v1/indexerproxy") for (m, u, b) in seen)
    assert "ADDED (direct): 1 - EZTV" in out


def test_already_present_and_absent_names_are_skipped_not_failed():
    out, _ = _run({
        ("GET", "/api/v1/tag"): [{"id": 3, "label": "flaresolverr"}],
        ("GET", "/api/v1/indexerproxy"): [{"id": 1, "implementation": "FlareSolverr"}],
        ("GET", "/api/v1/indexer/schema"): [{"name": "1337x", "fields": [], "implementation": "Cardigann"}],
        ("GET", "/api/v1/indexer"): [{"name": "1337x"}],   # already present
        ("POST", "/api/v1/indexer"): {"id": 9},
    })
    assert "1337x (already present)" in out and "not in schema" in out  # the rest aren't in schema
    assert "RESULT: added 0" in out and "FAILED" not in out


def test_a_non_cloudflare_failure_is_reported_not_retried():
    posts = []

    def post_indexer(body):
        posts.append(body)
        return uerr.HTTPError("u", 400, "Bad", {}, io.BytesIO(
            json.dumps([{"propertyName": "baseUrl", "errorMessage": "must not be empty"}]).encode()))

    out, _ = _run({
        ("GET", "/api/v1/tag"): [{"id": 3, "label": "flaresolverr"}],
        ("GET", "/api/v1/indexerproxy"): [{"id": 1, "implementation": "FlareSolverr"}],
        ("GET", "/api/v1/indexer/schema"): [{"name": "EZTV", "fields": [], "implementation": "Cardigann"}],
        ("GET", "/api/v1/indexer"): [],
        ("POST", "/api/v1/indexer"): post_indexer,
    })
    assert "FAILED: EZTV: " in out and "must not be empty" in out
    assert len([b for b in posts if b.get("name") == "EZTV"]) == 1, "a field-validation error is not retried via FlareSolverr"


def test_a_hang_on_a_post_is_recorded_not_fatal():
    def post_indexer(body):
        raise TimeoutError("timed out")

    out, _ = _run({
        ("GET", "/api/v1/tag"): [{"id": 3, "label": "flaresolverr"}],
        ("GET", "/api/v1/indexerproxy"): [{"id": 1, "implementation": "FlareSolverr"}],
        ("GET", "/api/v1/indexer/schema"): [{"name": "EZTV", "fields": [], "implementation": "Cardigann"},
                                            {"name": "1337x", "fields": [], "implementation": "Cardigann"}],
        ("GET", "/api/v1/indexer"): [],
        ("POST", "/api/v1/indexer"): post_indexer,
    })
    assert "FAILED:" in out and "unreachable" in out and "RESULT: added 0" in out


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
