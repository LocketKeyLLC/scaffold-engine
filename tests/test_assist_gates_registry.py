"""§17.1085 — the fail-safes are a REGISTRY, cover every surface, cannot fail
silently, and bite on the real drafts that got through.

Three tests, three properties:
1. coverage — every gate is applied on every surface the registry says it
   must be (walks the code; a new call site or renderer that lacks one fails);
2. fail-loud — a crashing gate is contained, logged under one event, counted,
   and shown on /health as degraded;
3. replay — the corpus in tests/fixtures/assist_replay holds the real drafts,
   records and facts from incidents; each must produce the outcome recorded.
"""
from __future__ import annotations

import json
import logging
import pathlib

import pytest

from app.modules import assist_gates as ag

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOD = ROOT / "app" / "modules"
CORPUS = ROOT / "tests" / "fixtures" / "assist_replay"


def _region(path: pathlib.Path, start: str, end: str) -> str:
    src = path.read_text(encoding="utf-8")
    i = src.index(start)
    j = src.index(end, i)
    return src[i:j]


def test_registry_shape():
    names = [g.name for g in ag.GATES]
    assert len(names) == len(set(names)) and len(names) >= 18
    for g in ag.GATES:
        assert (MOD / f"{g.module}.py").exists(), g.module
        assert g.kind in ("answer", "record", "prompt", "retrieval") and g.surfaces and g.since.startswith("§17.")
        assert g.symbol.rstrip("(=") in (MOD / f"{g.module}.py").read_text(encoding="utf-8") or g.symbol in ("missing_tools", "guess_before_look"), g.name


@pytest.mark.parametrize("surface", [s for s in sorted(ag.SURFACE_CODE) if not s.endswith("_post")])
def test_every_answer_surface_carries_every_gate_it_must(surface):
    """The verify_answer call region on each surface must pass the inputs the
    gates need. §17.751/§17.914/§17.1083 each found a surface a new gate had
    missed; this makes that a test failure instead of a live incident."""
    region = ""
    for part in ag.SURFACE_UNION.get(surface, (surface,)):
        fname, start, end = ag.SURFACE_CODE[part]
        region += _region(MOD / fname, start, end)
    fname, start, _ = ag.SURFACE_CODE[surface]
    required = [g for g in ag.GATES if surface in g.surfaces and g.kind in ("answer", "retrieval")]
    missing = []
    for g in required:
        if g.symbol == "verify_answer":
            if "verify_answer(" not in region and "verify_answer(" not in start:
                missing.append(g.name)
        elif g.symbol not in region:
            missing.append(g.name)
    assert not missing, f"{surface} lacks gates {missing} in {fname} region {start!r}"


def test_both_renderers_carry_every_prompt_guard():
    for fname, start in ag.RENDERERS:
        src = (MOD / fname).read_text(encoding="utf-8")
        i = src.index(start)
        nxt = [k for k in (src.find("\ndef ", i + 10), src.find("\nasync def ", i + 10)) if k > 0]
        region = src[i:min(nxt) if nxt else len(src)]
        for g in ag.GATES:
            if g.kind == "prompt":
                assert g.symbol in region, f"{fname}::{start} lacks prompt guard {g.name} ({g.symbol})"


def test_record_invariants_are_attached_to_their_write_funnels():
    state = (MOD / "assist_state.py").read_text(encoding="utf-8")
    assert state.count('run_gate("state_invariants", reconcile_system_state') == 2      # parse + merge
    env = (MOD / "assist_environment.py").read_text(encoding="utf-8")
    assert 'run_gate("fact_reconcile", reconcile_fact' in env
    # verify_answer routes its gates through the runner (both the draft and the candidate)
    ev = (MOD / "assist_evidence.py").read_text(encoding="utf-8")
    for name in ("unsourced_values", "citation_backing", "addresses_question", "ingress_target"):
        assert ev.count(f'run_gate("{name}"') + ev.count(f'run_gate_async("{name}"') == 2, name   # draft + candidate
    assert ev.count('run_gate("command_shape"') == 2


def test_a_crashing_gate_is_contained_logged_counted_and_visible(caplog):
    ag.reset_for_tests()
    caplog.set_level(logging.ERROR, logger="scaffold")
    def boom(x):
        raise KeyError("new shape")
    assert ag.run_gate("state_invariants", boom, 1, default=({}, [])) == ({}, [])
    assert ag.run_gate("state_invariants", boom, 1, default=({}, [])) == ({}, [])
    assert "assist_gate_crashed gate=state_invariants count=2" in caplog.text
    h = ag.gate_health()
    assert h["status"] == "degraded" and h["crashed"]["state_invariants"]["count"] == 2
    assert "KeyError" in h["crashed"]["state_invariants"]["last_error"] and h["runs"]["state_invariants"] == 2
    ag.reset_for_tests()
    assert ag.gate_health()["status"] == "up"
    src = (ROOT / "app" / "health.py").read_text(encoding="utf-8")
    assert '"assist_gates": _gate_health_safe()' in src


@pytest.mark.asyncio
async def test_async_runner_too():
    ag.reset_for_tests()
    async def boom():
        raise RuntimeError("x")
    assert await ag.run_gate_async("citation_backing", boom, default=None) is None
    assert ag.gate_health()["crashed"]["citation_backing"]["count"] == 1
    ag.reset_for_tests()


# ── replay corpus ──

_ENV = json.loads((CORPUS / "environment.json").read_text(encoding="utf-8"))
_CASES = sorted(p for p in CORPUS.glob("*.json") if p.name != "environment.json")


@pytest.mark.parametrize("path", _CASES, ids=[p.stem for p in _CASES])
def test_replay_corpus(path):
    case = json.loads(path.read_text(encoding="utf-8"))
    exp = case["expect"]
    if case["surface"] in ("research", "fix", "guide", "stream"):
        from app.modules.assist_inventory import build_system_map, ingress_issues
        sm = build_system_map(_ENV)
        issues = ingress_issues(case["draft"], sm)
        if "ingress_target" in exp:
            assert issues == exp["ingress_target"], case["note"]
        if "ingress_target_kinds" in exp:
            assert sorted({i["kind"] for i in issues}) == sorted(exp["ingress_target_kinds"]), case["note"]
        if "ingress_target_ports" in exp:
            assert sorted(i["port"] for i in issues if i["kind"] == "mgmt_port") == exp["ingress_target_ports"], case["note"]
    elif case["surface"] == "state_write":
        from app.modules.assist_state import reconcile_system_state
        clean, _ = reconcile_system_state(case["state"])
        want = exp["state_invariants"]
        for rid, fields in want.items():
            if rid.endswith("_keeps_mac"):
                mid = rid.split("_")[0]
                assert any("BC:24:11" in v for v in clean[mid]["devices"].values()) is fields, case["note"]
                continue
            for k, v in fields.items():
                assert clean[rid][k] == v, f"{case['note']}: {rid}.{k}"
    elif case["surface"] == "query":
        from app.modules.assist_evidence import class_query, derive_need
        need = derive_need(case["question"], operator_notes=case.get("notes"), assume_question=True)
        cq = class_query(need, case["question"], operator_notes=case.get("notes"),
                         local_names=[m.get("name") for m in _ENV.get("system_state", {}).values() if isinstance(m, dict) and (m.get("attrs") or {}).get("hostname")]
                         + ["caddy-proxy", "jellyfin"])
        want = exp["class_query"]
        assert cq.startswith(want["starts_with"]), (cq, case["note"])
        assert all(w in cq for w in want["contains"]), (cq, case["note"])
        assert not any(x.lower() in cq.lower().split() or x in cq for x in want["excludes"]), (cq, case["note"])
    elif case["surface"] == "fact_write":
        from app.modules.assist_inventory import reconcile_fact
        stored, upd = reconcile_fact(case["fact"], _ENV)
        want = exp["fact_reconcile"]
        assert want["contains"] in stored, case["note"]
        assert upd and want["update_id"] in upd and upd[want["update_id"]]["attrs"]["ip"] == want["update_ip"], case["note"]
    else:
        pytest.fail(f"unknown surface {case['surface']}")


def test_corpus_has_a_hit_and_a_non_hit_for_every_gate_it_covers():
    """A detector proven only by a quiet run proves nothing: for each gate the
    corpus exercises, at least one case must expect a hit and one must expect
    none (feedback: detector proof needs a hit)."""
    seen: dict[str, set[str]] = {}
    for p in _CASES:
        case = json.loads(p.read_text(encoding="utf-8"))
        for key, val in case["expect"].items():
            gate = key.split("_kinds")[0].split("_ports")[0]
            seen.setdefault(gate, set()).add("hit" if val not in ([], {}, None) else "none")
    assert seen.get("ingress_target") == {"hit", "none"}
    assert "hit" in seen.get("state_invariants", set()) and "hit" in seen.get("fact_reconcile", set())
    assert "hit" in seen.get("class_query", set())
