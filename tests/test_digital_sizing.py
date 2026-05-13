"""
Unit tests for ``app.sim.digital_sizing`` — Verilator-in-the-loop
sizing.

Both the LLM (``model_router.chat``) and the Verilator wrapper
(``run_verilator``) are mocked. Tests focus on the same control-
flow surfaces as ``test_device_sizing.py``: budget exhaustion,
convergence on first hit, feedback-driven retry, refusal paths, and
the persistence-on-attempt rule.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.providers.base import ModelResponse
from app.sim import digital_sizing as ds_mod
from app.sim.device_sizing import (
    CandidateIndexError,
    TopologySelectionNotFoundError,
)
from app.sim.digital_sizing import (
    DEFAULT_TOP_MODULE,
    DigitalSizingResult,
    size_digital_device,
)
from app.sim.spec_store import SpecRow
from app.sim.verilator import VerilatorResult
from tests.conftest import make_mock_db


SEL_ID = uuid.uuid4()
SPEC_ID = uuid.uuid4()


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
                "id": "wrap_count",
                "kind": "timing.latency",
                "description": "Cycles to first wrap.",
                "target": 16.0,
                "tolerance_pct": 5.0,
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


def _patch_topology_fetch(monkeypatch, *, exists: bool = True, candidates=None):
    async def _fake(db, sid):
        if not exists:
            raise TopologySelectionNotFoundError(f"{sid} not found")
        return _selection_row(candidates)
    # Patched on device_sizing because digital_sizing imports it from there.
    monkeypatch.setattr(
        "app.sim.digital_sizing._fetch_topology_selection", _fake
    )


def _patch_require_confirmed(monkeypatch, row: SpecRow):
    async def _fake(db, sid):
        from app.sim.spec_store import SpecNotConfirmedError
        if not row.is_confirmed:
            raise SpecNotConfirmedError(row.id)
        return row
    monkeypatch.setattr(
        "app.sim.digital_sizing.require_confirmed_spec", _fake
    )


def _patch_chat(monkeypatch, responses: list[ModelResponse]):
    mock = AsyncMock(side_effect=responses)
    monkeypatch.setattr(
        "app.sim.digital_sizing.model_router.chat", mock
    )
    return mock


def _patch_verilator(monkeypatch, results: list[VerilatorResult]):
    mock = AsyncMock(side_effect=results)
    monkeypatch.setattr(
        "app.sim.digital_sizing.run_verilator", mock
    )
    return mock


def _patch_insert(monkeypatch):
    async def _fake(db, **kwargs):
        return uuid.uuid4()
    mock = AsyncMock(side_effect=_fake)
    monkeypatch.setattr(
        "app.sim.digital_sizing._insert_digital_sizing", mock
    )
    return mock


def _llm_proposal(params: dict, sv: str = "module tb; endmodule\n") -> ModelResponse:
    return ModelResponse(
        text=json.dumps({"params": params, "sv_source": sv}),
        model="qwen3-vl:235b-instruct-cloud",
        success=True,
    )


def _verilator_ok(measurements: dict) -> VerilatorResult:
    return VerilatorResult(
        ok=True,
        exit_code=0,
        stdout="KPI wrap_count=16",
        stderr="",
        measurements=measurements,
        duration_ms=200,
        tool_version="verilator-5.024",
        timed_out=False,
        build_failed=False,
        seed=None,
        netlist_sha256="x",
        sim_run_id=uuid.uuid4(),
    )


def _verilator_failed(stderr: str = "build failed") -> VerilatorResult:
    return VerilatorResult(
        ok=False,
        exit_code=1,
        stdout="",
        stderr=stderr,
        measurements={},
        duration_ms=50,
        tool_version="verilator-5.024",
        timed_out=False,
        build_failed=True,
        seed=None,
        netlist_sha256="x",
        sim_run_id=uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_size_digital_converges_first_iteration(monkeypatch):
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    chat = _patch_chat(monkeypatch, [
        _llm_proposal({"WIDTH": "4", "CLK_PERIOD_NS": "10"}),
    ])
    ver = _patch_verilator(monkeypatch, [_verilator_ok({"wrap_count": 16.0})])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await size_digital_device(SEL_ID, db=db)

    assert result.ok is True
    assert result.converged is True
    assert result.iterations == 1
    assert result.top_module == DEFAULT_TOP_MODULE
    assert chat.await_count == 1
    assert ver.await_count == 1
    assert ins.await_count == 1
    insert_kwargs = ins.await_args.kwargs
    assert insert_kwargs["converged"] is True
    assert insert_kwargs["iterations"] == 1
    assert insert_kwargs["top_module"] == DEFAULT_TOP_MODULE
    assert len(insert_kwargs["sim_run_ids"]) == 1


@pytest.mark.smoke
async def test_size_digital_converges_iter_2_after_gap(monkeypatch):
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    _patch_chat(monkeypatch, [
        _llm_proposal({"WIDTH": "8"}),  # iter 1 wraps at 256
        _llm_proposal({"WIDTH": "4"}),  # iter 2 wraps at 16
    ])
    _patch_verilator(monkeypatch, [
        _verilator_ok({"wrap_count": 256.0}),  # out of tolerance
        _verilator_ok({"wrap_count": 16.0}),
    ])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await size_digital_device(SEL_ID, db=db)
    assert result.converged is True
    assert result.iterations == 2
    assert len(result.sim_run_ids) == 2


@pytest.mark.smoke
async def test_size_digital_budget_exhausted_persists_row(monkeypatch):
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    _patch_chat(monkeypatch, [
        _llm_proposal({"WIDTH": "8"}),
        _llm_proposal({"WIDTH": "8"}),
        _llm_proposal({"WIDTH": "8"}),
    ])
    _patch_verilator(monkeypatch, [
        _verilator_ok({"wrap_count": 256.0}),
        _verilator_ok({"wrap_count": 256.0}),
        _verilator_ok({"wrap_count": 256.0}),
    ])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await size_digital_device(SEL_ID, db=db, max_iterations=3)
    assert result.ok is False
    assert result.converged is False
    assert result.iterations == 3
    assert len(result.sim_run_ids) == 3
    assert ins.await_count == 1
    assert ins.await_args.kwargs["converged"] is False


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_verilator_failure_feeds_back_to_llm(monkeypatch):
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    _patch_chat(monkeypatch, [
        _llm_proposal({"WIDTH": "4"}, sv="broken sv source"),
        _llm_proposal({"WIDTH": "4"}, sv="fixed sv source"),
    ])
    _patch_verilator(monkeypatch, [
        _verilator_failed(stderr="syntax error at line 1"),
        _verilator_ok({"wrap_count": 16.0}),
    ])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await size_digital_device(SEL_ID, db=db)
    assert result.converged is True
    assert result.iterations == 2
    # Both sim_runs captured (§17.140 audit invariant — even failures
    # get a sim_runs row).
    assert len(result.sim_run_ids) == 2


@pytest.mark.smoke
async def test_llm_proposal_failure_continues_loop(monkeypatch):
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    _patch_chat(monkeypatch, [
        ModelResponse(text="garbage", model="x", success=True),
        _llm_proposal({"WIDTH": "4"}),
    ])
    _patch_verilator(monkeypatch, [
        _verilator_ok({"wrap_count": 16.0}),  # only iter 2 reaches verilator
    ])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await size_digital_device(SEL_ID, db=db)
    assert result.converged is True
    assert result.iterations == 2
    assert len(result.sim_run_ids) == 1


# ---------------------------------------------------------------------------
# Gate / refusal paths
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_unconfirmed_spec_refuses(monkeypatch):
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row(confirmed=False))
    chat = _patch_chat(monkeypatch, [])
    ver = _patch_verilator(monkeypatch, [])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await size_digital_device(SEL_ID, db=db)
    assert result.ok is False
    assert any("not confirmed" in e for e in result.errors)
    assert chat.await_count == 0
    assert ver.await_count == 0
    assert ins.await_count == 0


@pytest.mark.smoke
async def test_non_digital_kind_refused(monkeypatch):
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row(kind="analog_circuit"))
    chat = _patch_chat(monkeypatch, [])
    ver = _patch_verilator(monkeypatch, [])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await size_digital_device(SEL_ID, db=db)
    assert result.ok is False
    assert any("digital_logic" in e for e in result.errors)
    assert chat.await_count == 0
    assert ver.await_count == 0
    assert ins.await_count == 0


@pytest.mark.smoke
async def test_topology_missing_raises():
    db = make_mock_db()
    with pytest.raises(TopologySelectionNotFoundError):
        await size_digital_device(uuid.uuid4(), db=db)


@pytest.mark.smoke
async def test_candidate_idx_oob_raises(monkeypatch):
    _patch_topology_fetch(monkeypatch, candidates=[{"name": "X"}])
    db = make_mock_db()
    with pytest.raises(CandidateIndexError):
        await size_digital_device(SEL_ID, db=db, candidate_idx=5)
