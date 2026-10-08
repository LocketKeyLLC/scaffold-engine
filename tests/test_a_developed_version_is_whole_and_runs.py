"""§17.1417 — a developed version is accepted only WHOLE, and is run before it is offered.

Live, 2026-10-07, ADD123 round 3: the model wrote `'Type must be "movie" or "tv"'` with the inner
quotes unescaped inside a JSON string. `generate_json`'s lenient repair kept the text before the
break: the route file stopped at `'X-Api-Key': apiKey`, and server.js -- the file that must load
the route -- was gone. That fragment scored 0 (every gate passed) and was offered `suggested: run`.
Approved, the route module would not even parse and nothing would serve /api/media-request.

Two holes, two fixes: (A) only a strictly valid answer is a version; (B) a step whose own check is a
request to its service's route is rehearsed -- the sandbox runs the delivery, starts the service and
sends that request; a 404 or no answer refuses the version.
"""
from __future__ import annotations

import asyncio
import inspect
import json
import pathlib
from types import SimpleNamespace

from app.utils.llm_parsing import parse_json_object
from app.modules import develop as dv
from app.modules import execution_agent
from app.modules import rehearsal as rh

FIX = pathlib.Path(__file__).parent / "fixtures"
RAW = (FIX / "add123_develop_answer_unescaped_quote_2026_10_07.txt").read_text()
ADD123 = json.loads((FIX / "add123_node_2026_10_07.json").read_text())


def _propose_with(raw: str):
    from app import model_router

    async def fake(prompt, schema, **kw):
        return parse_json_object(raw), SimpleNamespace(text=raw, success=True)

    orig = model_router.generate_json
    model_router.generate_json = fake
    try:
        return asyncio.run(dv.propose("x"))
    finally:
        model_router.generate_json = orig


# ── A ────────────────────────────────────────────────────────────────────────

def test_the_live_answer_was_repaired_into_a_fragment():
    """The defect itself, on the real answer: the repair yields a truncated file and no server.js."""
    got = parse_json_object(RAW)
    # the repair returns [file, file, a stray string, a stray list]; propose kept the two dicts
    assert [type(f).__name__ for f in got["files"]] == ["dict", "dict", "str", "list"]
    files = {f["path"]: f["content"] for f in got["files"] if isinstance(f, dict)}
    route = files["/opt/control-panel-backend/media_request_route.js"]
    assert route.rstrip().endswith("'X-Api-Key': apiKey")
    assert "/opt/control-panel-backend/server.js" not in files


def test_a_broken_answer_is_no_version_and_says_where_it_broke():
    files, why = _propose_with(RAW)
    assert files is None
    assert "not valid JSON" in why and "movie" in why and '\\"' in why


def test_a_whole_answer_is_a_version_even_fenced_or_wrapped():
    content = "const s = 'Type " + '"movie"' + "';\n"
    ok = json.dumps({"files": [{"path": "/opt/x/a.js", "content": content}], "summary": "s"})
    for raw in (ok, f"```json\n{ok}\n```", f"Here it is:\n{ok}\nDone."):
        files, _ = _propose_with(raw)
        assert files == {"/opt/x/a.js": content}, raw[:30]


# ── B ────────────────────────────────────────────────────────────────────────

def test_the_steps_own_post_becomes_a_sandbox_request():
    reqs = rh.acceptance_requests(dv.acceptance_checks(ADD123))
    assert reqs == [{"method": "POST", "url": "http://127.0.0.1:3001/api/media-request",
                     "body": '{"title":"Sintel","type":"movie"}'}]


def test_a_call_to_another_machine_is_not_a_sandbox_request():
    assert rh.acceptance_requests(["pct exec 103 -- curl -f -X POST -d '{}' http://192.168.1.22:7878/api/v3/movie"]) == []


def _report(status, body, server_log=""):
    return {"commands": [], "probes": [], "roundtrip": None, "server_log": server_log,
            "accept": [{"method": "POST", "url": "http://127.0.0.1:3001/api/media-request",
                        "status": status, "body": body, "ok": bool(status) and status != 404}]}


def test_a_404_refuses_and_names_the_missing_load():
    refs = rh.refusal_from(_report(404, "Cannot POST /api/media-request"))
    assert len(refs) == 1 and rh.MARK in refs[0]["why"]
    assert "entry file never loads the module" in refs[0]["why"]
    assert rh.score(_report(404, "x")) == 3000


def test_no_answer_refuses_with_what_the_service_said():
    refs = rh.refusal_from(_report(0, "ConnectionRefusedError", "SyntaxError: Unexpected end of input"))
    assert "is not running" in refs[0]["why"] and "SyntaxError: Unexpected end of input" in refs[0]["why"]
    assert rh.score(_report(0, "x")) == 5000


def test_an_answer_about_the_unreachable_api_passes():
    """Radarr cannot be reached from the sandbox: the route answering 500 ABOUT that is the route working."""
    assert rh.refusal_from(_report(500, '{"error":"connect EHOSTUNREACH 192.168.1.22:7878"}')) == []
    assert rh.score(_report(500, "x")) == 0


def test_the_roundtrip_path_is_unchanged_by_it():
    assert rh.refusal_from({"commands": [], "roundtrip": {"ok": True}, "accept": []}) == []


def test_the_loop_rehearses_a_developed_step_with_its_acceptance_requests():
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert "rehearsal.acceptance_requests(develop.own_checks(run_node))" in src
    assert "accept=_rh_accept" in src


def test_the_sandbox_sends_them():
    import pytest
    f = pathlib.Path(__file__).parents[1] / "docker/rehearsal/rehearse.py"
    if not f.exists():          # `make test` mounts app/ and tests/ only; CI's checkout has docker/
        pytest.skip("docker/ is not mounted in this lane; the real sandbox was driven in §17.1417")
    src = f.read_text()
    assert 'job.get("accept")' in src and '"ok": bool(st) and st != 404' in src


def test_the_sandbox_gets_stand_in_key_files_for_the_keys_a_delivery_reads():
    from app.modules import service_truth as st
    seeds = rh.stand_in_keys([st.ServiceTruth(guest="103", name="radarr"), st.ServiceTruth(guest="111", name="control-panel"),
                              st.ServiceTruth(guest="101", name="jellyfin")])
    assert [s["path"] for s in seeds] == ["/var/lib/radarr/config.xml"]       # jellyfin's key is a db read, not a file
    assert f"<ApiKey>{rh.STAND_IN_KEY}</ApiKey>" in seeds[0]["content"]
    src = inspect.getsource(execution_agent._pause_for_decision)
    assert "rehearsal.stand_in_keys(_services)" in src


def test_the_listing_prunes_node_modules_in_the_find_and_reads_as_read_only():
    """Live: CT 111's unpruned listing was 317 lines of node_modules, cut off at `/opt/` before
    `scaffold-kit/`; `-prune` with parentheses is refused by the read channel, `-not -path` is not."""
    from app.modules.assist_state_check import read_only_command
    cmd = rh.listing_command("pct exec 111 --", "/opt/control-panel-backend")
    assert '-not -path "*/node_modules/*"' in cmd and "-prune" not in cmd and "(" not in cmd
    assert read_only_command(cmd)
    for mod in (dv.read_workspace, rh.seeds_for):
        assert "listing_command(" in inspect.getsource(mod)


def test_the_steps_own_request_failing_in_the_block_is_not_the_versions_fault():
    """Real sandbox: the block's own `curl -f` POST exits 22 in there (the route's answer is an error about
    the unreachable API) -- it must not be named as the failure, nor counted."""
    rep = _report(404, "Cannot POST /api/media-request")
    rep["commands"] = [{"command": "bash /tmp/scaffold-dev-ADD123--deliver.sh", "exit": 0, "out": ""},
                       {"command": "pct exec 111 -- curl -f -s -X POST -d '{}' http://127.0.0.1:3001/api/media-request",
                        "exit": 22, "out": ""}]
    why = rh.refusal_from(rep)[0]["why"]
    assert "exited 22" not in why and "answered 404" in why
    assert rh.score(rep) == 3000
    rep["accept"][0].update(status=502, ok=True)
    assert rh.refusal_from(rep) == [] and rh.score(rep) == 0
