"""
Unit tests for ``app.sim.formal_verify`` — symbiyosys-in-the-loop formal
verification with closed-loop repair (§17.414).

Both the LLM (``model_router.chat``) and the symbiyosys wrapper
(``run_symbiyosys``) are mocked. Tests cover the same control-flow surfaces
as ``test_digital_sizing.py`` (convergence, feedback retry, budget exhaustion,
refusal paths) PLUS the §17.414 property-locking rule: once a real verdict
(PASS/FAIL) is reached, the property harness is frozen and the LLM's later
``properties`` field is ignored.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.providers.base import ModelResponse
from app.sim import formal_verify as fv_mod
from app.sim.device_sizing import (
    CandidateIndexError,
    TopologySelectionNotFoundError,
)
from app.sim.formal_verify import (
    DEFAULT_FORMAL_TOP,
    DigitalSizingNotFoundError,
    verify_design,
)
from app.sim.spec_store import SpecRow
from app.sim.symbiyosys import SymbiYosysResult
from tests.conftest import make_mock_db


SEL_ID = uuid.uuid4()
SPEC_ID = uuid.uuid4()
DSID = uuid.uuid4()


def _spec_json(kind: str = "digital_logic") -> dict:
    return {
        "schema_version": "1.0.0",
        "design": {
            "name": "Counter",
            "kind": kind,
            "description": "N-bit counter that wraps at 2^N.",
        },
        "constraints": [
            {
                "id": "no_overflow",
                "kind": "timing.latency",
                "description": "count never exceeds 2^WIDTH-1.",
                "max": 15.0,
                "unit": "cycles",
                "criticality": "required",
            },
        ],
    }


def _spec_row(*, confirmed: bool = True, kind: str = "digital_logic") -> SpecRow:
    return SpecRow(
        id=SPEC_ID,
        job_id=None,
        schema_version="1.0.0",
        spec_json=_spec_json(kind=kind),
        spec_sha256="abc",
        confirmed_by="api_key" if confirmed else None,
        confirmed_at=datetime.now(timezone.utc) if confirmed else None,
        created_at=datetime.now(timezone.utc),
    )


def _selection_row(candidates: list[dict] | None = None) -> dict:
    return {
        "id": SEL_ID,
        "spec_id": SPEC_ID,
        "candidates": candidates if candidates is not None else [
            {
                "name": "Synchronous counter",
                "description": "Wrapping counter.",
                "rationale": "Standard.",
                "citations": ["chunk-A"],
            }
        ],
        "rag_chunk_ids": ["chunk-A"],
    }


def _patch_digital_sizing(monkeypatch, *, exists: bool = True, converged: bool = True):
    async def _fake(db, did):
        if not exists:
            raise DigitalSizingNotFoundError(f"{did} not found")
        return {
            "id": DSID,
            "spec_id": SPEC_ID,
            "topology_selection_id": SEL_ID,
            "candidate_idx": 0,
            "final_sv_source": "module dut(input clk); endmodule",
            "top_module": "tb",
            "converged": converged,
        }
    monkeypatch.setattr("app.sim.formal_verify._fetch_digital_sizing", _fake)


def _patch_topology_fetch(monkeypatch, *, candidates=None):
    async def _fake(db, sid):
        return _selection_row(candidates)
    monkeypatch.setattr("app.sim.formal_verify._fetch_topology_selection", _fake)


def _patch_require_confirmed(monkeypatch, row: SpecRow):
    async def _fake(db, sid):
        from app.sim.spec_store import SpecNotConfirmedError
        if not row.is_confirmed:
            raise SpecNotConfirmedError(row.id)
        return row
    monkeypatch.setattr("app.sim.formal_verify.require_confirmed_spec", _fake)


def _patch_chat(monkeypatch, responses: list[ModelResponse]):
    mock = AsyncMock(side_effect=responses)
    monkeypatch.setattr("app.sim.formal_verify.model_router.chat", mock)
    return mock


def _patch_sby(monkeypatch, results: list[SymbiYosysResult]):
    mock = AsyncMock(side_effect=results)
    monkeypatch.setattr("app.sim.formal_verify.run_symbiyosys", mock)
    return mock


def _patch_insert(monkeypatch):
    async def _fake(db, **kwargs):
        return uuid.uuid4()
    mock = AsyncMock(side_effect=_fake)
    monkeypatch.setattr("app.sim.formal_verify._insert_formal_verification", mock)
    return mock


def _llm(dut: str = "module dut(input clk); endmodule",
         properties: str = "module formal_top; assert property(1); endmodule") -> ModelResponse:
    return ModelResponse(
        text=json.dumps({"dut": dut, "properties": properties}),
        model="qwen3-vl:235b-instruct-cloud",
        success=True,
    )


def _sby(verdict: str, *, depth_reached=None, stderr: str = "") -> SymbiYosysResult:
    ok = verdict == "PASS"
    return SymbiYosysResult(
        ok=ok,
        verdict=verdict,
        exit_code=0 if ok else 1,
        stdout="",
        stderr=stderr,
        duration_ms=100,
        tool_version="symbiyosys-0.40",
        timed_out=False,
        seed=None,
        depth_reached=depth_reached,
        counterexample_vcd_b64=None,
        netlist_sha256="x",
        sim_run_id=uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_verify_passes_first_iteration(monkeypatch):
    _patch_digital_sizing(monkeypatch)
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    chat = _patch_chat(monkeypatch, [_llm()])
    sby = _patch_sby(monkeypatch, [_sby("PASS", depth_reached=20)])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await verify_design(DSID, db=db)

    assert result.ok is True
    assert result.converged is True
    assert result.verdict == "PASS"
    assert result.iterations == 1
    assert result.top_module == DEFAULT_FORMAL_TOP
    assert chat.await_count == 1
    assert sby.await_count == 1
    assert ins.await_count == 1
    k = ins.await_args.kwargs
    assert k["converged"] is True
    assert k["verdict"] == "PASS"
    assert len(k["sim_run_ids"]) == 1


@pytest.mark.smoke
async def test_fail_then_repair_passes(monkeypatch):
    _patch_digital_sizing(monkeypatch)
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    _patch_chat(monkeypatch, [
        _llm(dut="module dut_v1; endmodule"),
        _llm(dut="module dut_v2; endmodule"),
    ])
    _patch_sby(monkeypatch, [
        _sby("FAIL", depth_reached=7),
        _sby("PASS", depth_reached=20),
    ])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await verify_design(DSID, db=db)
    assert result.converged is True
    assert result.iterations == 2
    assert len(result.sim_run_ids) == 2


# ---------------------------------------------------------------------------
# Property-locking (the §17.414 anti-gaming rule)
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_properties_frozen_after_first_real_verdict(monkeypatch):
    """After a FAIL (a real verdict), iteration 2's LLM-supplied properties
    are IGNORED — the frozen harness is reused verbatim. The DUT may change."""
    _patch_digital_sizing(monkeypatch)
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    _patch_chat(monkeypatch, [
        _llm(dut="module dut_v1; endmodule",
             properties="module formal_top; LOCKED_P1 assert property(1); endmodule"),
        # iter 2 tries to weaken the properties — must be ignored.
        _llm(dut="module dut_v2; endmodule",
             properties="module formal_top; WEAK_P2 endmodule"),
    ])
    sby = _patch_sby(monkeypatch, [
        _sby("FAIL", depth_reached=3),
        _sby("PASS", depth_reached=20),
    ])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await verify_design(DSID, db=db)
    assert result.converged is True

    # iter 2's sv_source (positional arg 0) must carry the LOCKED properties,
    # not the weakened ones.
    iter2_sv = sby.await_args_list[1].args[0]
    assert "LOCKED_P1" in iter2_sv
    assert "WEAK_P2" not in iter2_sv
    # iter 2's DUT change DID take effect.
    assert "dut_v2" in iter2_sv
    # The persisted properties_source is the frozen harness.
    assert "LOCKED_P1" in ins.await_args.kwargs["properties_source"]
    assert "WEAK_P2" not in ins.await_args.kwargs["properties_source"]


@pytest.mark.smoke
async def test_error_allows_property_revision_before_lock(monkeypatch):
    """An ERROR (non-compiling SVA) before any real verdict does NOT freeze
    properties — iteration 2 may supply a corrected harness."""
    _patch_digital_sizing(monkeypatch)
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    _patch_chat(monkeypatch, [
        _llm(properties="module formal_top; BROKEN_SVA"),
        _llm(properties="module formal_top; FIXED_SVA assert property(1); endmodule"),
    ])
    sby = _patch_sby(monkeypatch, [
        _sby("ERROR", stderr="syntax error in assertion"),
        _sby("PASS", depth_reached=20),
    ])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await verify_design(DSID, db=db)
    assert result.converged is True
    iter2_sv = sby.await_args_list[1].args[0]
    assert "FIXED_SVA" in iter2_sv  # revision took effect (not yet locked)


# ---------------------------------------------------------------------------
# Budget / non-convergence
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_budget_exhausted_persists_row(monkeypatch):
    _patch_digital_sizing(monkeypatch)
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    _patch_chat(monkeypatch, [_llm(), _llm(), _llm()])
    _patch_sby(monkeypatch, [
        _sby("FAIL", depth_reached=2),
        _sby("FAIL", depth_reached=2),
        _sby("FAIL", depth_reached=2),
    ])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await verify_design(DSID, db=db, max_iterations=3)
    assert result.ok is False
    assert result.converged is False
    assert result.verdict == "FAIL"
    assert result.iterations == 3
    assert len(result.sim_run_ids) == 3
    assert ins.await_args.kwargs["converged"] is False


@pytest.mark.smoke
async def test_sidecar_unreachable_handled(monkeypatch):
    """run_symbiyosys returns verdict=ERROR (never raises) on an unreachable
    sidecar; the loop runs to budget and persists a non-converged row."""
    _patch_digital_sizing(monkeypatch)
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    _patch_chat(monkeypatch, [_llm(), _llm()])
    _patch_sby(monkeypatch, [
        _sby("ERROR", stderr="symbiyosys sidecar unreachable"),
        _sby("ERROR", stderr="symbiyosys sidecar unreachable"),
    ])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await verify_design(DSID, db=db, max_iterations=2)
    assert result.ok is False
    assert result.converged is False
    assert result.iterations == 2


@pytest.mark.smoke
async def test_llm_proposal_failure_continues_loop(monkeypatch):
    _patch_digital_sizing(monkeypatch)
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    _patch_chat(monkeypatch, [
        ModelResponse(text="garbage not json", model="x", success=True),
        _llm(),
    ])
    sby = _patch_sby(monkeypatch, [_sby("PASS", depth_reached=20)])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await verify_design(DSID, db=db)
    assert result.converged is True
    assert result.iterations == 2
    assert sby.await_count == 1  # only iter 2 reached the solver


# ---------------------------------------------------------------------------
# Gate / refusal paths
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_unconfirmed_spec_refuses(monkeypatch):
    _patch_digital_sizing(monkeypatch)
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row(confirmed=False))
    chat = _patch_chat(monkeypatch, [])
    sby = _patch_sby(monkeypatch, [])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await verify_design(DSID, db=db)
    assert result.ok is False
    assert any("not confirmed" in e for e in result.errors)
    assert chat.await_count == 0
    assert sby.await_count == 0
    assert ins.await_count == 0


@pytest.mark.smoke
async def test_non_digital_kind_refused(monkeypatch):
    _patch_digital_sizing(monkeypatch)
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row(kind="analog_circuit"))
    chat = _patch_chat(monkeypatch, [])
    sby = _patch_sby(monkeypatch, [])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await verify_design(DSID, db=db)
    assert result.ok is False
    assert any("digital_logic" in e for e in result.errors)
    assert chat.await_count == 0
    assert sby.await_count == 0
    assert ins.await_count == 0


@pytest.mark.smoke
async def test_digital_sizing_missing_raises(monkeypatch):
    _patch_digital_sizing(monkeypatch, exists=False)
    db = make_mock_db()
    with pytest.raises(DigitalSizingNotFoundError):
        await verify_design(DSID, db=db)


@pytest.mark.smoke
async def test_candidate_idx_oob_raises(monkeypatch):
    _patch_digital_sizing(monkeypatch)
    _patch_topology_fetch(monkeypatch, candidates=[])  # 0 out of range
    db = make_mock_db()
    with pytest.raises(CandidateIndexError):
        await verify_design(DSID, db=db)


# ---------------------------------------------------------------------------
# Prompt guards
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_system_prompt_enforces_no_weakening_and_formal_top():
    prompt = fv_mod._SYSTEM_PROMPT
    assert "formal_top" in prompt
    # The anti-gaming instruction must be present.
    assert "weaken" in prompt.lower()
    assert "FROZEN" in prompt or "frozen" in prompt
    # §17.417 — empirical guardrails verified against the live sidecar. The
    # Yosys `read -formal` frontend (no Verific) rejects concurrent SVA and
    # there is no sidecar-supplied clock; a regression that drops these
    # lessons sends the LLM straight back to verdict=ERROR.
    assert "always @(posedge clk)" in prompt          # immediate-assert pattern
    assert "read -formal" in prompt                    # toolchain awareness
    assert "does NOT support concurrent" in prompt     # the v2 ERROR lesson
    assert "Do NOT generate a clock" in prompt         # the clock-gen lesson
    assert "$display" in prompt                         # told NOT to use it
