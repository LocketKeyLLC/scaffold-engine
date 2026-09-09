"""§17.495 — unit tests for the model A/B harness scoring/aggregation.

Pure logic only (no model calls); `score_codegen` reuses the real
`check_golden` structural checker, so these also pin that integration.
"""
from __future__ import annotations

import pytest

from scripts.model_ab import (
    _avg, _summarize, score_codegen, score_extraction, score_routing,
    score_verifier, TASKS,
)


_GOLDEN = {
    "id": "module-function",
    "brief": "Write generate_filename(prefix, ext).",
    "must_parse": True,
    "must_define": ["generate_filename"],
    "must_not_contain": ["argparse", "__main__"],
}
_GOOD = "```python\ndef generate_filename(prefix: str, ext: str) -> str:\n    return f'{prefix}.{ext}'\n```"


def test_score_codegen_pass():
    s = score_codegen(_GOLDEN, _GOOD, exec_verdict="pass")
    assert s["passed"] is True
    assert s["structural_failures"] == []


def test_score_codegen_structural_fail_blocks_pass():
    bad = "```python\ndef other(): pass\n```"  # missing generate_filename
    s = score_codegen(_GOLDEN, bad, exec_verdict="pass")
    assert s["passed"] is False
    assert any("generate_filename" in f for f in s["structural_failures"])


def test_score_codegen_exec_fail_blocks_pass():
    # structural ok, but the sandbox said the code does not run
    s = score_codegen(_GOLDEN, _GOOD, exec_verdict="fail")
    assert s["passed"] is False


def test_score_codegen_exec_skip_does_not_block():
    # skip = couldn't run standalone (e.g. sibling import) — not held against it
    s = score_codegen(_GOLDEN, _GOOD, exec_verdict="skip")
    assert s["passed"] is True


def test_score_codegen_empty_output_fails():
    s = score_codegen(_GOLDEN, "", exec_verdict="skip")
    assert s["passed"] is False
    assert s["structural_failures"] == ["empty output"]


def test_summarize_aggregates_pass_error_metrics():
    rows = [
        {"model": "A", "ok": True, "passed": True, "wall_s": 2.0, "tokens_per_sec": 30.0, "ttft_ms": 800},
        {"model": "A", "ok": True, "passed": False, "wall_s": 4.0, "tokens_per_sec": 20.0, "ttft_ms": 1200},
        {"model": "A", "ok": False, "error": "boom"},
        {"model": "B", "ok": True, "passed": True, "wall_s": 1.0, "tokens_per_sec": 50.0, "ttft_ms": 400},
    ]
    s = _summarize(rows)
    assert s["A"]["trials"] == 3 and s["A"]["passed"] == 1 and s["A"]["errors"] == 1
    assert _avg(s["A"]["wall_s"]) == 3.0
    assert s["B"]["passed"] == 1 and s["B"]["errors"] == 0


def test_avg_empty_is_zero():
    assert _avg([]) == 0.0


# ── §17.495 — the critical correctness property: never score a fallback ──────


@pytest.mark.asyncio
async def test_run_one_rejects_fallback_used(monkeypatch):
    """generate() always computes a smart-fallback; an unavailable candidate
    that fell back must be reported unavailable, NEVER scored as the candidate."""
    from app import model_router
    from app.providers.base import ModelResponse
    from scripts.model_ab import _run_one

    async def _gen(prompt, model=None, **k):
        return ModelResponse(text=_GOOD, model="qwen3.5:397b-cloud",
                             success=True, fallback_used=True)
    monkeypatch.setattr(model_router, "generate", _gen)

    r = await _run_one(TASKS["codegen"], "qwen3-coder:480b-cloud", _GOLDEN,
                       temperature=0.2, max_tokens=512)
    assert r["ok"] is False and r["passed"] is False
    assert "fell back" in r["error"]


@pytest.mark.asyncio
async def test_run_one_rejects_model_mismatch(monkeypatch):
    """Even without fallback_used, a resolved model that differs is rejected."""
    from app import model_router
    from app.providers.base import ModelResponse
    from scripts.model_ab import _run_one

    async def _gen(prompt, model=None, **k):
        return ModelResponse(text=_GOOD, model="some-other-model",
                             success=True, fallback_used=False)
    monkeypatch.setattr(model_router, "generate", _gen)

    r = await _run_one(TASKS["codegen"], "candidate:cloud", _GOLDEN,
                       temperature=0.2, max_tokens=512)
    assert r["ok"] is False and "fell back" in r["error"]


@pytest.mark.asyncio
async def test_run_one_scores_matching_model(monkeypatch):
    """The requested model actually answered → it gets scored."""
    from app import model_router
    import app.sandbox.codegen_check as cc
    from app.providers.base import ModelResponse
    from app.sandbox.codegen_check import ExecCheckResult
    from scripts.model_ab import _run_one

    async def _gen(prompt, model=None, **k):
        return ModelResponse(text=_GOOD, model=model, success=True, fallback_used=False)

    async def _exec(output, **k):
        return ExecCheckResult("pass", "ran cleanly")

    monkeypatch.setattr(model_router, "generate", _gen)
    monkeypatch.setattr(cc, "codegen_exec_smoke", _exec)

    r = await _run_one(TASKS["codegen"], "qwen3.5:397b-cloud", _GOLDEN,
                       temperature=0.2, max_tokens=512)
    assert r["ok"] is True and r["passed"] is True
    assert r["resolved_model"] == "qwen3.5:397b-cloud"


# ── §17.496 — pre-flight availability check ─────────────────────────────────


@pytest.mark.asyncio
async def test_is_available_true_on_200(monkeypatch):
    import httpx
    from scripts.model_ab import _is_available

    class _Resp:
        status_code = 200

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
    assert await _is_available("qwen3.5:397b-cloud", "http://x:11434") is True


@pytest.mark.asyncio
async def test_is_available_false_only_on_404(monkeypatch):
    import httpx
    from scripts.model_ab import _is_available

    class _Resp:
        status_code = 404

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): return _Resp()

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
    assert await _is_available("nope:cloud", "http://x:11434") is False


@pytest.mark.asyncio
async def test_is_available_true_on_transient_error(monkeypatch):
    """A network hiccup must NOT false-skip a possibly-usable model."""
    import httpx
    from scripts.model_ab import _is_available

    class _Client:
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def post(self, *a, **k): raise RuntimeError("conn refused")

    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Client())
    assert await _is_available("maybe:cloud", "http://x:11434") is True


# ── §17.557 — extraction task scoring (native-or-coaxed entries-produced) ────


def test_score_extraction_pass_counts_dict_entries():
    s = score_extraction({"entries": [{"title": "a"}, {"title": "b"}]})
    assert s["passed"] is True and s["entries"] == 2
    assert s["metric"] == "entries" and s["metric_value"] == 2


def test_score_extraction_empty_entries_fails():
    s = score_extraction({"entries": []})
    assert s["passed"] is False and s["entries"] == 0


def test_score_extraction_none_args_fails():
    # read_tool_args returns None on a tool-call miss (the §17.556 failure mode)
    s = score_extraction(None)
    assert s["passed"] is False and s["entries"] == 0


def test_score_extraction_ignores_non_dict_entries():
    # the shape-drift case (§17.522): model returns strings, not objects
    s = score_extraction({"entries": ["just", "strings"]})
    assert s["passed"] is False and s["entries"] == 0


def test_extraction_task_registered():
    assert "extraction" in TASKS
    assert TASKS["extraction"].default_goldens.name == "extraction_goldens.json"


# §17.567 — verifier task (verdict-match)

def test_score_verifier_matching_pass():
    s = score_verifier({"pass": True, "reason": "ok", "confidence": 0.9}, "pass")
    assert s["passed"] is True and s["verdict"] == "pass"
    assert s["metric"] == "verdict_match" and s["metric_value"] == "pass"


def test_score_verifier_matching_fail():
    s = score_verifier({"pass": False, "reason": "missing", "confidence": 0.9}, "fail")
    assert s["passed"] is True and s["verdict"] == "fail"


def test_score_verifier_mismatch_fails():
    # model said pass, golden expected fail → verdict-match miss
    s = score_verifier({"pass": True, "reason": "x", "confidence": 0.5}, "fail")
    assert s["passed"] is False and s["verdict"] == "pass" and s["expected"] == "fail"


def test_score_verifier_no_toolcall_fails_closed():
    # read_tool_args returns None on a tool-call miss → fail-closed, verdict none
    s = score_verifier(None, "pass")
    assert s["passed"] is False and s["verdict"] == "none"


def test_score_verifier_missing_pass_key_fails_closed():
    s = score_verifier({"reason": "no pass key"}, "pass")
    assert s["passed"] is False and s["verdict"] == "none"


def test_verifier_task_registered():
    assert "verifier" in TASKS
    assert TASKS["verifier"].default_goldens.name == "verifier_goldens.json"


# §17.805 — routing task (route_command intent-match)

def test_score_routing_match():
    s = score_routing({"intent": "status"}, "status")
    assert s["passed"] is True and s["verdict"] == "status"
    assert s["metric"] == "intent_match" and s["metric_value"] == "status"


def test_score_routing_mismatch_fails():
    # model routed to 'none', golden expected 'status' → intent-match miss
    s = score_routing({"intent": "none"}, "status")
    assert s["passed"] is False and s["verdict"] == "none" and s["expected"] == "status"


def test_score_routing_no_toolcall_fails_closed():
    # read_tool_args returns None on a tool-call miss → fail-closed, verdict none
    s = score_routing(None, "status")
    assert s["passed"] is False and s["verdict"] == "none"


def test_score_routing_empty_intent_fails_closed():
    s = score_routing({"intent": ""}, "status")
    assert s["passed"] is False and s["verdict"] == "none"


def test_routing_task_registered():
    assert "routing" in TASKS
    assert TASKS["routing"].default_goldens.name == "routing_goldens.json"


def test_routing_goldens_valid_intents():
    # every golden's expected_intent must be a real router intent, else the
    # task can never pass (and the golden is a silent typo).
    from scripts.model_ab import _load_goldens
    from app.modules.command_guide import COMMAND_INTENTS
    goldens = _load_goldens(TASKS["routing"].default_goldens)
    assert len(goldens) >= 10
    assert all(g["expected_intent"] in COMMAND_INTENTS for g in goldens)


async def test_dispatch_routing_uses_route_tool_and_scores(monkeypatch):
    # _dispatch_routing must call model_router.tool_call with the live route_command
    # tool at temp 0.0; _score_routing then intent-matches. Mock the model call.
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    from scripts import model_ab
    fake_resp = SimpleNamespace(success=True,
                                tool_calls=[SimpleNamespace(arguments={"intent": "status"})])
    captured = {}

    async def _fake_tool_call(messages, tools, **kw):
        captured["tool_name"] = tools[0].name
        captured["temperature"] = kw.get("temperature")
        captured["model"] = kw.get("model")
        return fake_resp

    import app.model_router as mr
    monkeypatch.setattr(mr, "tool_call", AsyncMock(side_effect=_fake_tool_call))
    golden = {"message": "show me my active jobs", "expected_intent": "status"}
    resp = await model_ab._dispatch_routing(
        "qwen3.5:397b-cloud", golden, temperature=0.2, max_tokens=512)
    s = await model_ab._score_routing(golden, resp)
    assert captured["tool_name"] == "route_command"
    assert captured["temperature"] == 0.0        # harness temp ignored, prod is deterministic
    assert captured["model"] == "qwen3.5:397b-cloud"
    assert s["passed"] is True and s["verdict"] == "status"


# ── §17.992 — `expect: "empty"` goldens: refusing IS the correct answer ──


def test_score_extraction_expect_empty_passes_on_a_refusal():
    """§17.990 measured a corpus where the search engine returned the Greek
    titan, the 2012 Ridley Scott film and IMDb for `prometheus histogram
    buckets`. Refusing that is right; padding it with off-topic entries is
    worse than zero, because it makes a plan LOOK grounded on a topic it never
    addresses. The reliability-only scorer graded those exactly backwards."""
    s = score_extraction({"entries": []}, expect="empty")
    assert s["passed"] is True
    assert s["entries"] == 0
    assert s["expect"] == "empty"


def test_score_extraction_expect_empty_fails_a_padding_model():
    s = score_extraction(
        {"entries": [{"title": "prometheus-open-source-monitoring",
                      "content": "Prometheus is a monitoring toolkit"}]},
        expect="empty")
    assert s["passed"] is False, "off-topic padding must not score as a pass"


def test_score_extraction_expect_empty_passes_when_no_tool_call_at_all():
    assert score_extraction(None, expect="empty")["passed"] is True


def test_score_extraction_default_is_unchanged():
    """Every pre-§17.992 golden omits `expect` and must keep the original
    reliability meaning — non-empty entries is a pass."""
    assert score_extraction({"entries": [{"t": 1}]})["passed"] is True
    assert score_extraction({"entries": []})["passed"] is False
    assert score_extraction(None)["passed"] is False
    assert score_extraction({"entries": [{"t": 1}]})["expect"] == "entries"


def test_every_extraction_golden_declares_a_reachable_expectation():
    """A typo in `expect` would silently fall back to "entries" and invert the
    meaning of a refusal golden — the exact inversion this feature exists to
    fix."""
    import json
    import pathlib

    p = (pathlib.Path(__file__).parent / "fixtures" / "extraction_goldens.json")
    goldens = json.loads(p.read_text())["goldens"]
    assert len(goldens) >= 6, "the gate was starved at 2 goldens (§17.992)"
    for g in goldens:
        assert g.get("expect", "entries") in {"entries", "empty"}, g["id"]
        assert g.get("results"), g["id"]
    # Both directions must be represented, or the gate cannot catch a model
    # that either over-refuses or over-pads.
    kinds = {g.get("expect", "entries") for g in goldens}
    assert kinds == {"entries", "empty"}, kinds


# ── §17.993 — the research_extract task tests the REAL production contract ──


def _rx(**kw):
    e = {"title": "t", "content": "c", "tags": "", "source": "https://a.example/1",
         "confidence_score": 0.9, "source_type": "tech_docs"}
    e.update(kw)
    return e


def _score_rx(entries, urls=("https://a.example/1", "https://a.example/2"), **kw):
    from scripts.model_ab import score_research_extract
    return score_research_extract({"entries": entries}, set(urls), **kw)


def test_research_extract_accepts_a_well_formed_batch():
    v = _score_rx([_rx(), _rx(source="https://a.example/2", confidence_score=0.7)])
    assert v["passed"] is True, v["reason"]


def test_research_extract_rejects_a_source_type_outside_the_enum():
    v = _score_rx([_rx(source_type="official_doc")])   # singular — not the enum
    assert v["passed"] is False
    assert "source_type" in v["reason"]


def test_the_enum_is_read_from_the_tool_schema_not_transcribed():
    """§17.993 — the first cut hardcoded this set from a hand-read of
    EXTRACT_SYSTEM_V1 that had been truncated mid-word ("official_docs|curated"
    → "official_doc"). Every model then failed for emitting the CORRECT value,
    and it read like a finding about the models. Deriving it from the schema the
    model is actually shown makes that class of error impossible."""
    from scripts.model_ab import _allowed_source_types

    from app.modules.research_agent import RECORD_ENTRIES_TOOL

    allowed = _allowed_source_types()
    assert "official_docs" in allowed and "curated" in allowed
    assert "official_doc" not in allowed
    desc = (RECORD_ENTRIES_TOOL.input_schema["properties"]["entries"]["items"]
            ["properties"]["source_type"]["description"])
    assert allowed == {t.strip() for t in desc.split("|") if t.strip()}


def test_research_extract_rejects_a_source_that_was_not_in_the_batch():
    """§17.854 — fetched-page content can talk a model into attributing a fact
    to a high-trust URL that was never in the batch, which then earns a domain
    confidence boost it has not earned. The `source-attribution-pressure`
    golden plants exactly that bait; glm-5.3-flash took it 2/3."""
    v = _score_rx([_rx(source="https://kubernetes.io/docs/concepts/security/")])
    assert v["passed"] is False
    assert "source_not_in_batch" in v["reason"]


def test_research_extract_rejects_confidence_out_of_range():
    assert _score_rx([_rx(confidence_score=1.4)])["passed"] is False
    assert _score_rx([_rx(confidence_score="high")])["passed"] is False


def test_constant_confidence_only_fails_where_authority_actually_varies():
    """A constant confidence is evidence of miscalibration only on a
    mixed-authority corpus. The first cut applied it everywhere and failed
    models for the right answer: `official-docs-only` is three sqlite.org pages
    where 1.0 across the board is correct."""
    flat = [_rx(confidence_score=1.0),
            _rx(confidence_score=1.0, source="https://a.example/2")]
    assert _score_rx(flat)["passed"] is True
    v = _score_rx(flat, expect_graded_confidence=True)
    assert v["passed"] is False and "confidence_constant" in v["reason"]
    graded = [_rx(confidence_score=1.0),
              _rx(confidence_score=0.4, source="https://a.example/2")]
    assert _score_rx(graded, expect_graded_confidence=True)["passed"] is True


def test_recording_a_marketing_page_fails():
    """The direct test of "discard noise, opinions, marketing language" — schema
    checks alone cannot see it, because a dutifully-recorded slogan is perfectly
    well-formed."""
    v = _score_rx([_rx(source="https://example-cloud.com/")],
                  urls=("https://a.example/1", "https://example-cloud.com/"),
                  forbidden_sources={"https://example-cloud.com/"})
    assert v["passed"] is False
    assert "recorded_marketing" in v["reason"]


def test_research_extract_goldens_are_well_formed():
    import json
    import pathlib

    p = pathlib.Path(__file__).parent / "fixtures" / "research_extract_goldens.json"
    goldens = json.loads(p.read_text())["goldens"]
    assert len(goldens) >= 4
    for g in goldens:
        assert g.get("topic") and g.get("results"), g["id"]
        for u in (g.get("forbidden_sources") or []):
            assert any(r.get("url") == u for r in g["results"]), (g["id"], u)
    # Both discriminating properties must stay represented, or the gate silently
    # degrades back to a schema-only check.
    assert any(g.get("expect_graded_confidence") for g in goldens)
    assert any(g.get("forbidden_sources") for g in goldens)


def test_the_role_is_graded_on_the_task_it_actually_runs():
    """It was mapped to "extraction", which dispatches gt_extractor's DISTILL
    prompt and a 4-field tool — a strictly easier job than the 7-field
    `record_entries` this role runs in production."""
    from app.modules.model_role_learning import ROLE_TASKS

    assert ROLE_TASKS["model_research_extract"] == "research_extract"


# ── §17.994 — every task is claimed, every role's task exists ────────────


def test_every_role_maps_to_a_task_that_exists():
    from app.modules.model_role_learning import ROLE_TASKS

    unknown = {r: t for r, t in ROLE_TASKS.items() if t not in TASKS}
    assert not unknown, f"roles mapped to non-existent A/B tasks: {unknown}"


def test_no_task_is_orphaned():
    """A task no role references is a gate nobody runs. `extraction` became one
    the moment §17.993 gave model_research_extract its own task — which is how
    a carefully-grown golden set quietly stops being a gate at all."""
    from app.modules.model_role_learning import ROLE_TASKS

    claimed = set(ROLE_TASKS.values())
    orphaned = set(TASKS) - claimed
    assert not orphaned, (
        f"A/B tasks referenced by no role: {sorted(orphaned)} — either map a "
        "role to it or delete it; an unreferenced gate rots silently")


def test_model_general_is_graded_on_a_job_it_runs():
    """It was mapped to `routing` as a proxy. Its own highest-stakes job is the
    ideation distill — `gt_extractor.distill_entries` runs under
    `ideation_model_role`, which is model_general — and that is what the
    `extraction` task dispatches."""
    from app.config import settings
    from app.modules.model_role_learning import ROLE_TASKS

    assert settings.ideation_model_role == "model_general"
    assert ROLE_TASKS["model_general"] == "extraction"


# ── §17.995 — the model-pick RECORDS must not drift from the code ────────


def test_every_tuned_cloud_pick_names_a_model_the_repo_still_ships():
    """`app/config.py` records each role's tuned cloud pick in a comment. Those
    comments are the only durable record of an operator-local `.env` — a rebuild
    starts from the local-safe defaults (§17.819) and someone reads these to
    restore the measured configuration.

    Three of them went stale in one day: §17.994 corrected model_router and
    model_research_extract after swapping them, and §17.995 then found
    model_general still naming deepseek-v4-pro:cloud while the live pin had long
    since moved to gemma4:cloud. This pins the SHAPE — a pick must at least name
    a model the repo mentions somewhere else — so a pick referring to nothing
    real (a retired tag, a typo) fails here rather than at a rebuild.
    """
    import pathlib
    import re

    root = pathlib.Path(__file__).parent.parent
    cfg = (root / "app" / "config.py").read_text()
    picks = dict(re.findall(r"^\s*(model_\w+): str = .*?tuned cloud pick: ([^,)]+)",
                            cfg, re.M))
    assert picks, "the tuned-pick comments vanished — that record is load-bearing"
    # .env.example is not mounted into the dev test image, so read what is
    # present rather than depending on the lane's mount list.
    corpus = cfg
    for extra in ((root / ".env.example"),
                  (root / "app" / "modules" / "profiles.py")):
        if extra.exists():
            corpus += extra.read_text()
    for role, pick in picks.items():
        pick = pick.strip()
        assert pick in corpus, (
            f"{role}'s tuned cloud pick {pick!r} is named nowhere else in the "
            "repo — likely a stale or mistyped tag")


def test_the_quick_profile_pins_only_known_roles():
    """§17.995 — the quick profile is a second model map, tuned at a different
    time from config.py's, and it drifted the same way: it pinned
    gpt-oss:20b-cloud on routing and research_extract, both of which its own
    goldens now rate worse AND slower than alternatives that did not exist when
    it was written."""
    from app.config import SWITCHABLE_ROLE_FIELDS
    from app.modules.profiles import quick_model_map

    unknown = set(quick_model_map()) - set(SWITCHABLE_ROLE_FIELDS)
    assert not unknown, f"quick profile pins unknown roles: {unknown}"


# ── §17.997 — the triage task tests the prompts model_triage actually runs ──


_TRIAGE_OK = ("**Scope so far:** A CLI.\n\n**Options:**\n- A\n- B\n\n"
              "**Gaps:**\nWHAT: which formats?\n\n**My pick:** A.")


def _score_tri(text, **g):
    from scripts.model_ab import score_triage
    return score_triage(text, g)


def test_triage_accepts_the_four_section_contract():
    assert _score_tri(_TRIAGE_OK, mode="triage")["passed"] is True


def test_triage_rejects_a_dropped_my_pick():
    """TRIAGE_SYSTEM_PROMPT is explicit that "My pick" is never dropped, including
    when elaborating or answering a follow-up — the failure mode is a model
    treating a follow-up as a plain chat reply."""
    v = _score_tri(_TRIAGE_OK.replace("**My pick:** A.", ""), mode="triage")
    assert v["passed"] is False and "My pick" in v["reason"]


def test_triage_rejects_out_of_order_headers():
    swapped = ("**Gaps:**\nx\n\n**Scope so far:** y\n\n**Options:**\n- A\n\n"
               "**My pick:** A.")
    v = _score_tri(swapped, mode="triage")
    assert v["passed"] is False and v["reason"] == "headers_out_of_order"


def test_triage_rejects_components_on_a_single_focus_build():
    """`Components` is the one optional extra and only legal when the build
    genuinely splits into parts."""
    with_comp = _TRIAGE_OK.replace("**Options:**", "**Components:**\na — x\n\n**Options:**")
    assert _score_tri(with_comp, mode="triage", expect_components=True)["passed"] is True
    v = _score_tri(with_comp, mode="triage", expect_components=False)
    assert v["passed"] is False and "components_on_single_focus" in v["reason"]


def test_triage_catches_the_empty_response_that_started_this():
    """The incumbent returned success=True with ZERO characters on the first
    turn of a new chat — twice out of two, after ~90s — and production answers
    that with a canned "I couldn't reach the planner just now". The routing gate
    it was graded on could not see this at all: there it tied at 24/24."""
    v = _score_tri("", mode="triage")
    assert v["passed"] is False and v["reason"] == "empty"


def test_synthesis_fails_when_a_superseded_detail_survives():
    """§17.694 — the conversation is a TIMELINE. A problem the user later reports
    RESOLVED must not be carried, nor escalated into a from-scratch rebuild."""
    v = _score_tri("Plan: reinstall the OS on the Proxmox box.",
                   mode="synthesis", required=["proxmox"],
                   forbidden=["reinstall the os"])
    assert v["passed"] is False and "carried_superseded" in v["reason"]
    ok = _score_tri("Plan: clean up the old tenant on the running Proxmox host.",
                    mode="synthesis", required=["proxmox"],
                    forbidden=["reinstall the os"])
    assert ok["passed"] is True


def test_triage_goldens_cover_both_prompts():
    import json
    import pathlib

    p = pathlib.Path(__file__).parent / "fixtures" / "triage_goldens.json"
    goldens = json.loads(p.read_text())["goldens"]
    modes = {g.get("mode") for g in goldens}
    assert modes == {"triage", "synthesis"}, (
        "the role runs BOTH prompts; a gate covering one of them is how this "
        "role ended up graded on `routing` in the first place")
    for g in goldens:
        assert g.get("messages"), g["id"]
        if g["mode"] == "synthesis":
            assert g.get("forbidden") or g.get("required"), g["id"]
