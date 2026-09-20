"""§17.1138 (ledger L-4) — the state check survives a partial paste.

The 09-19 check handed the operator 47 probes in one script and resolved
with 43 unknown; the stored record kept counts only, so the cause (a partial
run, dropped markers) was unrecoverable. Now: a per-script budget with the
rest queued, a partial paste keeps the check pending for the uncovered ids
(next script = uncovered + deferred), "skip the rest" finishes with what was
answered, and the record says WHY each probe stayed unknown.
"""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.modules import assist_state_check as sc


def _probe(i: int, kind: str = "step") -> dict:
    return {"id": f"S:T{i}", "kind": kind, "claim": f"claim {i}", "command": f"echo {i}", "expect": "", "node_key": f"T{i}"}


class _Db:
    """Captures the JSON patches the module writes to the session row."""
    def __init__(self, meta: dict):
        self.meta = meta; self.patches: list[dict] = []; self.commit = AsyncMock()

    async def execute(self, stmt, params=None):
        params = params or {}
        if "patch" in params:
            self.patches.append(json.loads(params["patch"])); self.meta.update(self.patches[-1])
        if "r" in params:
            self.patches.append({"record": json.loads(params["r"])})
            self.meta.pop("pending_state_check", None)
        res = MagicMock(); res.scalar.return_value = self.meta; return res


def _pending(probes, deferred=(), verdicts=(), total=None):
    return {"ts": "t", "node_key": "T9", "probes": list(probes), "deferred": list(deferred),
            "verdicts": list(verdicts), "probes_total": total or (len(probes) + len(deferred) + len(verdicts)),
            "claims_total": 50}


def _judge_by_marker(probes, pasted):
    present = set(sc.attribute_sections(pasted))
    out = []
    for p in probes:
        if p["id"] in present:
            out.append({**p, "verdict": "confirmed", "reason": "seen"})
        else:
            out.append({**p, "verdict": "unknown", "reason": "no output pasted for this check"})
    return out


@pytest.mark.asyncio
async def test_a_partial_paste_keeps_the_check_pending_with_the_uncovered_and_deferred_probes():
    probes = [_probe(i) for i in range(1, 5)]; deferred = [_probe(i) for i in range(5, 8)]
    db = _Db({"pending_state_check": _pending(probes, deferred)})
    pasted = "== S:T1 ==\nok\n== S:T2 ==\nfine\n"
    with patch.object(sc, "judge_outputs", AsyncMock(side_effect=_judge_by_marker)):
        res = await sc.resolve_state_check(db=db, session_id="sid", pasted=pasted)
    assert res["pending"] is True and res["proposal"] is None
    nxt = db.meta["pending_state_check"]
    assert [p["id"] for p in nxt["probes"]] == ["S:T3", "S:T4", "S:T5", "S:T6", "S:T7"], "uncovered first, then deferred"
    assert nxt["deferred"] == [] and nxt["partial_pastes"] == 1
    assert [v["id"] for v in nxt["verdicts"]] == ["S:T1", "S:T2"], "only the answered ones accumulate"
    assert "2 of 7 answered so far" in res["message"] and "`S:T3`" in res["message"] and "echo 5" in res["message"]
    assert "skip the rest" in res["message"]


@pytest.mark.asyncio
async def test_the_second_paste_finalises_with_the_accumulated_verdicts_and_the_reason_breakdown():
    prior = [{**_probe(1), "verdict": "confirmed", "reason": "seen"}, {**_probe(2), "verdict": "contradicted", "reason": "gone"}]
    remaining = [_probe(3), _probe(4)]
    db = _Db({"pending_state_check": _pending(remaining, verdicts=prior, total=4)})
    with patch.object(sc, "judge_outputs", AsyncMock(side_effect=_judge_by_marker)), \
         patch("app.modules.assist_notes._stage_replan_proposal", AsyncMock(return_value=None), create=True), \
         patch("app.modules.assist_environment.set_environment", AsyncMock(), create=True):
        res = await sc.resolve_state_check(db=db, session_id="sid", pasted="== S:T3 ==\nyes\n== S:T4 ==\n")
    assert not res.get("pending")
    rec = next(p["record"] for p in db.patches if "record" in p)
    assert (rec["confirmed"], rec["contradicted"], rec["unknown"]) == (3, 1, 0)
    assert rec["probes_total"] == 4 and rec["unknown_no_output"] == 0 and rec["unknown_judge"] == 0
    assert "pending_state_check" not in db.meta


@pytest.mark.asyncio
async def test_skip_the_rest_finishes_with_what_was_answered_and_records_why():
    prior = [{**_probe(1), "verdict": "confirmed", "reason": "seen"}]
    db = _Db({"pending_state_check": _pending([_probe(2), _probe(3)], deferred=[_probe(4)], verdicts=prior, total=4)})
    with patch.object(sc, "judge_outputs", AsyncMock(side_effect=AssertionError("must not judge on finish"))):
        res = await sc.resolve_state_check(db=db, session_id="sid", pasted="", finish=True)
    rec = next(p["record"] for p in db.patches if "record" in p)
    assert rec["confirmed"] == 1 and rec["unknown"] == 3 and rec["unknown_skipped"] == 3
    assert not res.get("pending")


@pytest.mark.asyncio
async def test_a_full_paste_still_resolves_in_one_go():
    probes = [_probe(1), _probe(2)]
    db = _Db({"pending_state_check": _pending(probes)})
    with patch.object(sc, "judge_outputs", AsyncMock(side_effect=_judge_by_marker)):
        res = await sc.resolve_state_check(db=db, session_id="sid", pasted="== S:T1 ==\na\n== S:T2 ==\nb\n")
    assert not res.get("pending") and "pending_state_check" not in db.meta


@pytest.mark.asyncio
async def test_start_state_check_budgets_the_script_and_queues_the_rest(monkeypatch):
    from app.config import settings
    many = [_probe(i) for i in range(1, 31)]
    db = _Db({})
    monkeypatch.setattr(settings, "assist_state_check_max_probes", 24)
    with patch.object(sc, "build_claims", AsyncMock(return_value={"claims": [{"id": p["id"], "kind": "step", "text": p["claim"]} for p in many], "environment": {}, "current_node_key": "T1"})), \
         patch.object(sc, "plan_probes", AsyncMock(return_value=(many, []))), \
         patch("app.modules.assist_local_runner.runner_spec", AsyncMock(return_value=None), create=True):
        out = await sc.start_state_check(db=db, session_id="sid", node_key="T1")
    pend = db.meta["pending_state_check"]
    assert len(pend["probes"]) == 24 and len(pend["deferred"]) == 6 and pend["probes_total"] == 30
    assert "first 24 of 30 checks" in out["message"] and "next 6" in out["message"]


@pytest.mark.parametrize("text_,expected", [
    ("skip the rest", True), ("Skip remaining checks", True), ("that's all I have", True), ("finish the state check", True),
    ("skip", False), ("skip T3", False), ("skip this step", False), ("== S:T1 ==\nok", False),
])
def test_the_skip_phrase_never_eats_the_step_skip_command(text_, expected):
    assert bool(sc.STATE_CHECK_SKIP_RE.search(text_)) is expected
