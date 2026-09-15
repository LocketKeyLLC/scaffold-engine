"""§17.1078 — tests written AGAINST the second mutation pass's survivors.

`.profiles/mutmut-results2.txt` left 302 survivors in four functions:
proposals_from_verdicts (96), plan_probes (89), judge_outputs (62),
read_only_command (55). Each test here pins one of the things the survivor
diffs showed no test was looking at: an exact truncation length, a boundary
comparison (`>=` vs `>`), a specific curl flag, the arguments the model is
called with, a log line's text, a self-referencing `caused_by`. Equivalent
mutants (`wget` inside the curl tuple — `wget` is already a mutation head;
`zip(strict=…)` — the AST never returns unequal lists) are not chased.
"""
from __future__ import annotations

import logging
from unittest.mock import MagicMock

import pytest

from app import model_router
from app.modules import assist_state_check as sc

pytestmark = pytest.mark.asyncio


def _tool_resp(name: str, args: dict):
    r = MagicMock(); r.success = True; r.tool_calls = [{"name": name, "arguments": args}]; return r


@pytest.fixture
def model(monkeypatch):
    """Capture every tool_call; the queue of responses is consumed in order."""
    calls: list[dict] = []
    queue: list = []

    async def fake(**kw):
        calls.append(kw)
        if queue:
            nxt = queue.pop(0)
            if isinstance(nxt, Exception):
                raise nxt
            return nxt
        return _tool_resp("x", {})
    monkeypatch.setattr(model_router, "tool_call", fake)
    import app.utils.tool_call_args as tca
    monkeypatch.setattr(tca, "read_tool_args", lambda resp: resp.tool_calls[0]["arguments"] if getattr(resp, "tool_calls", None) else None)
    return {"calls": calls, "queue": queue}


# ── read_only_command ───────────────────────────────────────────────────────

@pytest.mark.parametrize("cmd", ["", "   ", "# just a comment", "#docker ps"])
def test_read_only_empty_and_comments_are_refused(cmd):
    assert sc.read_only_command(cmd) is False


@pytest.mark.parametrize("cmd", [
    "curl -X GET http://x", "curl --request GET http://x", "curl -d x http://x", "curl --data x http://x",
    "curl --data-raw x http://x", "curl --upload-file f http://x", "curl -T f http://x",
    "curl -o /tmp/out http://x", "curl -O http://x",
    "curl -o /dev/null -X POST http://x",          # /dev/null does not launder a method override
    "curl -o /dev/null -o /tmp/x http://x",         # ALL outputs must be /dev/null
    "curl http://x -o",                             # dangling -o: no target, refused, no crash
])
def test_read_only_each_curl_write_flag_is_refused(cmd):
    assert sc.read_only_command(cmd) is False


@pytest.mark.parametrize("cmd", [
    "curl -s http://x", "curl -sI http://x", "curl -o /dev/null -s -w '%{http_code}' http://x",
    "ls -o /tmp",           # the curl flag table applies to curl only — `-o` on ls is a listing flag
    "getent passwd -d",     # likewise -d on a non-curl head
])
def test_read_only_curl_read_shapes_and_non_curl_flags_pass(cmd):
    assert sc.read_only_command(cmd) is True


@pytest.mark.parametrize("cmd", ["systemctl start nginx", "pct destroy 101", "docker rm x", "git reset --hard",
                                 "ip link add x type dummy", "crontab -r", "echo $(rm -rf /x)"])
def test_read_only_mutation_heads_and_subcommands_are_refused(cmd):
    assert sc.read_only_command(cmd) is False


def test_read_only_second_gate_is_the_guide_scanner_on_a_fenced_block():
    """Passes the AST gate (`ls --color=auto` ; `b` are both harmless heads) and
    is refused ONLY by find_shell_unsafe_commands seeing the `;` inside an
    unquoted --flag=value — so a mutant that breaks the fence or the
    argument passed to the scanner lets it through."""
    assert sc.read_only_command("ls --color=auto") is True
    assert sc.read_only_command("ls --color=auto;b") is False


# ── plan_probes ─────────────────────────────────────────────────────────────

def _claims(n: int, kind="fact", prefix="F"):
    return [{"id": f"{prefix}:{i}", "kind": kind, "text": f"claim {i}"} for i in range(1, n + 1)]


async def test_plan_probes_prompt_carries_profile_600_and_pins_20_and_only_when_present(model):
    profile = "P" * 601
    pins = [{"id": f"K:{i}", "kind": "pin", "text": f"KEY{i} = v{i}"} for i in range(1, 22)]
    model["queue"].append(_tool_resp("plan_state_probes", {"probes": [{"id": "F:1", "command": "uptime"}]}))
    await sc.plan_probes(_claims(1) + pins, {"profile": profile})
    msg = model["calls"][0]["messages"][0]["content"]
    assert model["calls"][0]["messages"][0]["role"] == "user"
    assert "\n\nOPERATOR CONTEXT:\n" + "P" * 600 in msg and "P" * 601 not in msg
    assert "\n\nKNOWN VALUES (the operator's pins):\n- KEY1 = v1\n- KEY2 = v2" in msg
    assert "- KEY20 = v20" in msg and "- KEY21 = v21" not in msg           # [:20]
    assert "CLAIMS (give a probe for each of the 1, or an empty command):\n- F:1: claim 1" in msg
    # pins are context, never targets
    assert all("K:" not in ln.split(":")[0] for ln in msg.split("CLAIMS")[1].splitlines() if ln.startswith("- "))
    model["calls"].clear(); model["queue"].append(_tool_resp("plan_state_probes", {"probes": []}))
    await sc.plan_probes(_claims(1), None)
    msg2 = model["calls"][0]["messages"][0]["content"]
    assert "OPERATOR CONTEXT" not in msg2 and "KNOWN VALUES" not in msg2


async def test_plan_probes_model_call_arguments_are_exact(model):
    model["queue"].append(_tool_resp("plan_state_probes", {"probes": []}))
    await sc.plan_probes(_claims(1), None, model_overrides={"model_general": "m"})
    kw = model["calls"][0]
    assert kw["tools"] == [sc.PLAN_PROBES_TOOL] and kw["role"] == "model_general"
    assert kw["overrides"] == {"model_general": "m"} and kw["temperature"] == 0.0
    assert kw["tool_choice"] == "auto" and kw["max_tokens"] == 2500


async def test_plan_probes_truncates_command_300_expect_200_refused_120(model):
    long_ro = "echo " + "a" * 320
    long_mut = "rm -rf /" + "b" * 130
    model["queue"].append(_tool_resp("plan_state_probes", {"probes": [
        {"id": "F:1", "command": long_ro, "expect": "e" * 210},
        {"id": "F:2", "command": long_mut}]}))
    probes, refused = await sc.plan_probes(_claims(2), None)
    assert len(probes[0]["command"]) == 300 and probes[0]["command"] == long_ro[:300]
    assert len(probes[0]["expect"]) == 200
    assert probes[0]["claim"] == "claim 1" and probes[0]["kind"] == "fact" and probes[0]["node_key"] is None
    assert refused == [{"id": "F:2", "command": long_mut[:120]}] and len(refused[0]["command"]) == 120


async def test_plan_probes_stops_exactly_at_the_cap_without_another_batch(model):
    n = sc._MAX_PROBES
    first = [{"id": f"F:{i}", "command": f"echo {i}"} for i in range(1, n + 1)]
    model["queue"].append(_tool_resp("plan_state_probes", {"probes": first}))
    # 5 batches of 10 exist; the model returns the whole cap in batch 1 → no batch 2
    claims = _claims(n + 2)
    probes, _ = await sc.plan_probes(claims, None)
    assert len(probes) == n and len(model["calls"]) == 1


async def test_plan_probes_survives_a_non_dict_item_and_an_unknown_id(model):
    model["queue"].append(_tool_resp("plan_state_probes", {"probes": [
        123, {"id": "F:9", "command": "uptime"}, {"id": "F:1", "command": "  uptime   -p "}, {"id": "F:2", "command": ""}]}))
    probes, refused = await sc.plan_probes(_claims(2), None)
    assert [p["id"] for p in probes] == ["F:1"] and probes[0]["command"] == "uptime -p" and refused == []


async def test_plan_probes_logs_batches_counts_and_a_model_failure(model, caplog):
    caplog.set_level(logging.INFO, logger="scaffold")
    model["queue"].append(RuntimeError("boom"))
    model["queue"].append(_tool_resp("plan_state_probes", {"probes": [
        {"id": "F:11", "command": "uptime"}, {"id": "F:12", "command": "rm -rf " + "x" * 60}]}))
    claims = _claims(12)
    probes, refused = await sc.plan_probes(claims, None)
    text = caplog.text
    assert "state_check_probe_model_failed batch=1/2: RuntimeError('boom')" in text
    assert "state_check_probe_batch 1/2 claims=10 probes=0" in text
    assert "state_check_probe_batch 2/2 claims=2 probes=1" in text
    assert "state_check_probes claims=12 targets=12 probes=1 refused=[('F:12', 'rm -rf " in text
    assert "'rm -rf " + "x" * 43 + "')" in text          # command[:50] inside the refused tuple
    assert [p["id"] for p in probes] == ["F:11"]


async def test_plan_probes_refused_log_lists_at_most_four(model, caplog):
    caplog.set_level(logging.INFO, logger="scaffold")
    model["queue"].append(_tool_resp("plan_state_probes", {"probes": [
        {"id": f"F:{i}", "command": f"rm -rf /d{i}"} for i in range(1, 6)]}))
    _, refused = await sc.plan_probes(_claims(5), None)
    assert len(refused) == 5
    line = next(ln for ln in caplog.text.splitlines() if "state_check_probes claims=5" in ln)
    assert line.count("('F:") == 4 and "('F:5'" not in line


# ── judge_outputs ───────────────────────────────────────────────────────────

def _probe(cid, claim="c", command="uptime", expect="", kind="fact", node_key=None):
    return {"id": cid, "claim": claim, "command": command, "expect": expect, "kind": kind, "node_key": node_key}


async def test_judge_defaults_when_no_marker_at_all(model):
    v = await sc.judge_outputs([_probe("F:1", node_key="T2")], "nothing pasted")
    assert v == [{"id": "F:1", "verdict": "unknown", "reason": "no output pasted for this check",
                  "repair": "", "kind": "fact", "node_key": "T2", "claim": "c", "title": ""}]
    assert model["calls"] == []


async def test_judge_expected_text_prepass_boundary_is_six_chars_and_reason_clips_80(model):
    model["queue"].append(_tool_resp("record_state_verdicts", {"verdicts": []}))
    exp81 = "x" * 81
    probes = [_probe("F:1", expect="abcdef"), _probe("F:2", expect="abcde"), _probe("F:3", expect=exp81)]
    pasted = "== F:1 ==\nzz abcdef zz\n== F:2 ==\nzz abcde zz\n== F:3 ==\n" + exp81
    v = {x["id"]: x for x in await sc.judge_outputs(probes, pasted)}
    assert v["F:1"]["verdict"] == "confirmed" and v["F:1"]["reason"] == "output contains the expected 'abcdef'"
    assert v["F:2"]["verdict"] == "unknown" and v["F:2"]["reason"] == "the judge returned no verdict for this check"
    assert v["F:3"]["reason"] == f"output contains the expected '{'x' * 80}'"
    block = model["calls"][0]["messages"][0]["content"]
    assert "[F:2] CLAIM" in block and "[F:1]" not in block and "[F:3]" not in block


async def test_judge_prompt_block_fields_empty_marker_and_1200_clip(model):
    model["queue"].append(_tool_resp("record_state_verdicts", {"verdicts": []}))
    long_out = "o" * 1210
    probes = [_probe("S:T1", claim="claim one", command="docker ps", expect="", kind="step", node_key="T1"),
              _probe("F:2", claim="claim two", command="ss -tlnp", expect="LISTEN 3001")]
    pasted = "== S:T1 ==\n\n== F:2 ==\n" + long_out
    await sc.judge_outputs(probes, pasted)
    kw = model["calls"][0]
    block = kw["messages"][0]["content"]
    assert kw["messages"][0]["role"] == "user"
    assert "Give one verdict for EACH of the 2 claims, in order." in block
    assert ("[S:T1] CLAIM: claim one\nCOMMAND: docker ps\nEXPECTED WHEN TRUE: (unspecified)\nOUTPUT:\n"
            "(no output — the command printed nothing)") in block
    assert "[F:2] CLAIM: claim two\nCOMMAND: ss -tlnp\nEXPECTED WHEN TRUE: LISTEN 3001\nOUTPUT:\n" + "o" * 1200 in block
    assert "o" * 1201 not in block
    assert kw["tools"] == [sc.RECORD_VERDICTS_TOOL] and kw["role"] == "model_general" and kw["overrides"] is None
    assert kw["temperature"] == 0.0 and kw["tool_choice"] == "auto" and kw["max_tokens"] == 3000


async def test_judge_accepts_lowercase_verdicts_only_and_clips_reason_repair_300(model):
    model["queue"].append(_tool_resp("record_state_verdicts", {"verdicts": [
        "junk", {"id": "F:1", "verdict": "CONFIRMED"},
        {"id": "F:1", "verdict": "contradicted", "reason": "r" * 310, "repair": "restart the thing " + "p" * 300, "caused_by": " F:2 "},
        {"id": "F:9", "verdict": "confirmed"}]}))
    v = await sc.judge_outputs([_probe("F:1")], "== F:1 ==\nout")
    assert v[0]["verdict"] == "contradicted" and len(v[0]["reason"]) == 300
    assert len(v[0]["repair"]) == 300 and v[0]["repair"].startswith("restart the thing ")
    assert v[0]["caused_by"] == "F:2"


async def test_judge_destructive_repair_is_dropped_and_logged(model, caplog):
    caplog.set_level(logging.INFO, logger="scaffold")
    rep = "Remove the container and " + "z" * 100
    model["queue"].append(_tool_resp("record_state_verdicts", {"verdicts": [
        {"id": "F:1", "verdict": "contradicted", "reason": "gone", "repair": rep}]}))
    v = await sc.judge_outputs([_probe("F:1")], "== F:1 ==\nout")
    assert v[0]["repair"] == "" and v[0]["needs_decision"] is True and v[0]["verdict"] == "contradicted"
    assert f"state_check_repair_refused id=F:1 repair={rep[:80]!r}" in caplog.text


async def test_judge_model_failure_is_logged_and_leaves_unknown(model, caplog):
    caplog.set_level(logging.WARNING, logger="scaffold")
    model["queue"].append(RuntimeError("boom"))
    v = await sc.judge_outputs([_probe("F:1")], "== F:1 ==\nout")
    assert v[0]["verdict"] == "unknown" and "state_check_judge_model_failed: RuntimeError('boom')" in caplog.text


# ── proposals_from_verdicts ─────────────────────────────────────────────────

def _v(cid, verdict="contradicted", kind="fact", **kw):
    d = {"id": cid, "verdict": verdict, "kind": kind, "claim": f"claim {cid}", "reason": f"reason {cid}", "repair": ""}
    d.update(kw); return d


def test_proposals_caused_by_self_or_a_non_contradicted_claim_does_not_fold():
    vs = [_v("F:1", repair="Start nginx", caused_by="F:1"),                  # self-reference: keep
          _v("F:2", repair="Start redis", caused_by="F:9"),                  # F:9 not contradicted: keep
          _v("F:3", repair="Start milvus", caused_by="F:1"),                 # consequence of F:1: fold
          _v("F:9", verdict="confirmed")]
    out = sc.proposals_from_verdicts(vs, anchor_node_key="T5")
    assert [p["proposed_change"] for p in out] == ["Start nginx", "Start redis"]


def test_proposals_step_repair_fields_and_exact_clips():
    v = _v("S:T3", kind="step", node_key="T3", claim="c" * 310, reason="r" * 310, repair="Start caddy " + "q" * 400)
    out = sc.proposals_from_verdicts([v], anchor_node_key=None)
    assert out == [{"node_key": "T3", "action": "repair", "current_assumption": "c" * 300,
                    "proposed_change": ("Start caddy " + "q" * 400)[:400], "reason": "r" * 300}]
    assert len(out[0]["proposed_change"]) == 400


def test_proposals_step_reopen_text_and_200_clip():
    v = _v("S:T3", kind="step", node_key="T3", title="Configure the reverse proxy", claim="Configured caddy for 3001",
           reason="x" * 210)
    out = sc.proposals_from_verdicts([v], anchor_node_key=None)
    assert out == [{"node_key": "T3", "action": "reopen", "current_assumption": "Configured caddy for 3001",
                    "proposed_change": "Redo this step — " + "x" * 200}]


def test_proposals_reopen_refused_marks_and_logs_title_80(caplog):
    caplog.set_level(logging.INFO, logger="scaffold")
    title = "Stop the control-panel backend " + "t" * 80
    v = _v("S:T9", kind="step", node_key="T9", title=title, claim="Ran systemctl stop")
    out = sc.proposals_from_verdicts([v], anchor_node_key=None)
    assert out == [] and v["needs_decision"] == "reopen"
    assert f"state_check_reopen_refused id=S:T9 title={title[:80]!r}" in caplog.text


def test_proposals_indirect_contradiction_marks_and_logs_reason_80(caplog):
    caplog.set_level(logging.INFO, logger="scaffold")
    reason = "grep found no references to the site; " + "w" * 80
    v = _v("F:4", reason=reason, repair="Complete the first-time setup")
    out = sc.proposals_from_verdicts([v], anchor_node_key="T2")
    assert out == [] and v["needs_decision"] == "indirect"
    assert f"state_check_indirect_contradiction id=F:4 reason={reason[:80]!r}" in caplog.text


def test_proposals_fact_repair_needs_an_anchor_and_carries_reason():
    v = _v("F:1", repair="Start nginx", reason="not listening")
    assert sc.proposals_from_verdicts([dict(v)], anchor_node_key=None) == []
    assert sc.proposals_from_verdicts([_v("F:1")], anchor_node_key="T2") == []      # no repair → only retracted
    out = sc.proposals_from_verdicts([dict(v)], anchor_node_key="T2")
    assert out == [{"node_key": "T2", "action": "repair", "current_assumption": "claim F:1",
                    "proposed_change": "Start nginx", "reason": "not listening"}]


def test_proposals_destructive_repair_never_leaves():
    v = _v("F:1", repair="Delete the old volume and start again")
    assert sc.proposals_from_verdicts([v], anchor_node_key="T2") == []


def test_proposals_fold_threshold_is_exactly_half_overlap():
    # terms(a) = {uptime, kuma, 3001} ; terms(b) = {uptime, kuma, 3002, again?} → overlap 2 / min(3, 3)
    a = _v("F:1", repair="Start uptime-kuma on 3001")
    b = _v("F:2", repair="Start uptime-kuma on 3002")                    # 2/3 ≥ 0.5 → folded
    c = _v("F:3", repair="Start milvus-standalone on 19530")            # 0 overlap → kept
    out = sc.proposals_from_verdicts([a, b, c], anchor_node_key="T2")
    assert [p["proposed_change"] for p in out] == ["Start uptime-kuma on 3001", "Start milvus-standalone on 19530"]
    # exactly 0.5: {nginx, 8080} vs {nginx, 9090} → 1/2 — a `> 0.5` mutant keeps both
    d = _v("F:4", repair="Start nginx 8080")
    e = _v("F:5", repair="Start nginx 9090")
    assert len(sc.proposals_from_verdicts([d, e], anchor_node_key="T2")) == 1


def test_proposals_repairs_with_no_identifier_terms_are_both_kept():
    """§17.1071 finding, pinned: 'Start it' and 'Run it' have no ≥4-char
    non-stopword tokens, so they can never fold — and must not crash the
    overlap division either."""
    out = sc.proposals_from_verdicts([_v("F:1", repair="Start it"), _v("F:2", repair="Run it")], anchor_node_key="T2")
    assert len(out) == 2


def test_proposals_reopen_dedupe_needs_same_node_and_same_text():
    same = dict(kind="step", title="Configure the proxy", claim="Configured", reason="gone")
    a = _v("S:T3", node_key="T3", **same); b = _v("S:T3b", node_key="T3", **same)
    c = _v("S:T4", node_key="T4", **same)
    d = _v("S:T3c", node_key="T3", **dict(same, reason="different"))
    out = sc.proposals_from_verdicts([a, b, c, d], anchor_node_key=None)
    assert [(p["node_key"], p["proposed_change"]) for p in out] == [
        ("T3", "Redo this step — gone"), ("T4", "Redo this step — gone"), ("T3", "Redo this step — different")]
