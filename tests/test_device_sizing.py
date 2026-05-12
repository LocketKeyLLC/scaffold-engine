"""
Unit tests for ``app.sim.device_sizing`` — closed-loop sizing stage.

Both the LLM (``model_router.chat``) and the ngspice wrapper
(``run_ngspice``) are mocked. The tests focus on the loop's control
flow: budget exhaustion, convergence on first hit, feedback-driven
retry, refusal paths, and the persistence-on-attempt rule.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.providers.base import ModelResponse
from app.sim import device_sizing as ds_mod
from app.sim.device_sizing import (
    CandidateIndexError,
    DeviceSizingResult,
    TopologySelectionNotFoundError,
    _check_constraints,
    size_device,
)
from app.sim.ngspice import NgspiceResult
from app.sim.spec_store import SpecRow
from tests.conftest import make_mock_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

SEL_ID = uuid.uuid4()
SPEC_ID = uuid.uuid4()


def _spec_json(kind: str = "analog_circuit") -> dict:
    return {
        "schema_version": "1.0.0",
        "design": {
            "name": "RC LPF",
            "kind": kind,
            "description": "First-order RC low-pass.",
        },
        "constraints": [
            {
                "id": "fc_3db",
                "kind": "electrical.frequency",
                "description": "Corner.",
                "target": 1000.0,
                "tolerance_pct": 5.0,
                "unit": "Hz",
                "criticality": "required",
            },
        ],
    }


def _spec_row(*, confirmed: bool = True, kind: str = "analog_circuit") -> SpecRow:
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
                "name": "RC low-pass",
                "description": "First-order RC.",
                "rationale": "Trivial fit.",
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
    monkeypatch.setattr(
        "app.sim.device_sizing._fetch_topology_selection", _fake
    )


def _patch_require_confirmed(monkeypatch, row: SpecRow):
    async def _fake(db, sid):
        from app.sim.spec_store import SpecNotConfirmedError
        if not row.is_confirmed:
            raise SpecNotConfirmedError(row.id)
        return row
    monkeypatch.setattr(
        "app.sim.device_sizing.require_confirmed_spec", _fake
    )


def _patch_chat(monkeypatch, responses: list[ModelResponse]):
    """Patch model_router.chat to return responses[i] on call i."""
    mock = AsyncMock(side_effect=responses)
    monkeypatch.setattr(
        "app.sim.device_sizing.model_router.chat", mock
    )
    return mock


def _patch_ngspice(monkeypatch, results: list[NgspiceResult]):
    mock = AsyncMock(side_effect=results)
    monkeypatch.setattr(
        "app.sim.device_sizing.run_ngspice", mock
    )
    return mock


def _patch_insert(monkeypatch):
    async def _fake(db, **kwargs):
        return uuid.uuid4()
    mock = AsyncMock(side_effect=_fake)
    monkeypatch.setattr(
        "app.sim.device_sizing._insert_sizing", mock
    )
    return mock


def _llm_proposal(params: dict, netlist: str = "* netlist\n.end\n") -> ModelResponse:
    return ModelResponse(
        text=json.dumps({"params": params, "netlist": netlist}),
        model="qwen3-vl:235b-instruct-cloud",
        success=True,
    )


def _ngspice_ok(measurements: dict) -> NgspiceResult:
    return NgspiceResult(
        ok=True,
        exit_code=0,
        stdout="",
        stderr="",
        measurements=measurements,
        duration_ms=42,
        tool_version="ngspice-44.2",
        timed_out=False,
        seed=None,
        netlist_sha256="x",
        sim_run_id=uuid.uuid4(),
    )


def _ngspice_failed(stderr: str = "broken syntax") -> NgspiceResult:
    return NgspiceResult(
        ok=False,
        exit_code=1,
        stdout="",
        stderr=stderr,
        measurements={},
        duration_ms=10,
        tool_version="ngspice-44.2",
        timed_out=False,
        seed=None,
        netlist_sha256="x",
        sim_run_id=uuid.uuid4(),
    )


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_size_device_converges_first_iteration(monkeypatch):
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    chat = _patch_chat(monkeypatch, [_llm_proposal({"R1": "1k", "C1": "159.155n"})])
    ng = _patch_ngspice(monkeypatch, [_ngspice_ok({"fc_3db": 1000.0})])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await size_device(SEL_ID, db=db)

    assert result.ok is True
    assert result.converged is True
    assert result.iterations == 1
    assert chat.await_count == 1
    assert ng.await_count == 1
    assert ins.await_count == 1
    insert_kwargs = ins.await_args.kwargs
    assert insert_kwargs["converged"] is True
    assert insert_kwargs["iterations"] == 1
    assert len(insert_kwargs["sim_run_ids"]) == 1


@pytest.mark.smoke
async def test_size_device_converges_on_second_iteration_after_gap(monkeypatch):
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    _patch_chat(monkeypatch, [
        _llm_proposal({"R1": "10k", "C1": "10n"}),   # iter 1 miss
        _llm_proposal({"R1": "1k",  "C1": "159.155n"}),  # iter 2 hit
    ])
    _patch_ngspice(monkeypatch, [
        _ngspice_ok({"fc_3db": 1591.5}),  # 60% high — out of tolerance
        _ngspice_ok({"fc_3db": 1003.0}),  # within ±5%
    ])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await size_device(SEL_ID, db=db)

    assert result.ok is True
    assert result.converged is True
    assert result.iterations == 2
    assert len(result.sim_run_ids) == 2
    # The second iter's params persisted as final, not the first.
    assert result.final_params == {"R1": "1k", "C1": "159.155n"}
    assert ins.await_count == 1


@pytest.mark.smoke
async def test_size_device_budget_exhausted_persists_row(monkeypatch):
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    _patch_chat(monkeypatch, [
        _llm_proposal({"R1": "10k", "C1": "10n"}),
        _llm_proposal({"R1": "10k", "C1": "10n"}),
        _llm_proposal({"R1": "10k", "C1": "10n"}),
    ])
    _patch_ngspice(monkeypatch, [
        _ngspice_ok({"fc_3db": 5000.0}),  # far from 1000
        _ngspice_ok({"fc_3db": 5000.0}),
        _ngspice_ok({"fc_3db": 5000.0}),
    ])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await size_device(SEL_ID, db=db, max_iterations=3)

    assert result.ok is False
    assert result.converged is False
    assert result.iterations == 3
    assert len(result.sim_run_ids) == 3
    # Row is still persisted — the §17.147 audit-the-attempt rule.
    assert ins.await_count == 1
    insert_kwargs = ins.await_args.kwargs
    assert insert_kwargs["converged"] is False
    assert any("budget exhausted" in e for e in insert_kwargs["errors"])


# ---------------------------------------------------------------------------
# Failure paths — LLM proposal failure, ngspice failure
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_llm_proposal_failure_continues_loop_and_persists(monkeypatch):
    """LLM emits unparseable JSON on iter 1; iter 2 recovers. The
    iter-1 degenerate record still appears in the history so an
    auditor can see what happened."""
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    _patch_chat(monkeypatch, [
        ModelResponse(text="garbage not json", model="x", success=True),
        _llm_proposal({"R1": "1k", "C1": "159.155n"}),
    ])
    _patch_ngspice(monkeypatch, [
        # Iter 1's LLM failure means no ngspice call — only iter 2 hits.
        _ngspice_ok({"fc_3db": 1000.0}),
    ])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await size_device(SEL_ID, db=db)

    assert result.converged is True
    assert result.iterations == 2  # both attempts recorded
    assert len(result.sim_run_ids) == 1  # only iter 2 reached ngspice
    assert ins.await_count == 1


@pytest.mark.smoke
async def test_ngspice_failure_in_iter_feeds_back_to_llm(monkeypatch):
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row())
    _patch_chat(monkeypatch, [
        _llm_proposal({"R1": "1k", "C1": "1u"}, netlist="* broken\n.bad\n.end"),
        _llm_proposal({"R1": "1k", "C1": "159.155n"}),
    ])
    _patch_ngspice(monkeypatch, [
        _ngspice_failed(stderr="ngspice: syntax error at line 2"),
        _ngspice_ok({"fc_3db": 1000.0}),
    ])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await size_device(SEL_ID, db=db)

    assert result.converged is True
    assert result.iterations == 2
    # Both sim_runs recorded — including the failed one (the §17.140
    # wrapper's audit invariant guarantees a sim_runs row even on
    # ngspice failure).
    assert len(result.sim_run_ids) == 2


# ---------------------------------------------------------------------------
# Gate / refusal paths — no row persisted
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_unconfirmed_spec_refuses_no_row(monkeypatch):
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row(confirmed=False))
    ng = _patch_ngspice(monkeypatch, [])
    ch = _patch_chat(monkeypatch, [])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await size_device(SEL_ID, db=db)

    assert result.ok is False
    assert any("not confirmed" in e for e in result.errors)
    assert ch.await_count == 0
    assert ng.await_count == 0
    assert ins.await_count == 0


@pytest.mark.smoke
async def test_non_analog_kind_refused_no_row(monkeypatch):
    _patch_topology_fetch(monkeypatch)
    _patch_require_confirmed(monkeypatch, _spec_row(kind="digital_logic"))
    ng = _patch_ngspice(monkeypatch, [])
    ch = _patch_chat(monkeypatch, [])
    ins = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await size_device(SEL_ID, db=db)

    assert result.ok is False
    assert any("analog_circuit" in e for e in result.errors)
    assert ch.await_count == 0
    assert ng.await_count == 0
    assert ins.await_count == 0


@pytest.mark.smoke
async def test_topology_selection_missing_raises():
    """Programmer error, not runtime data — router maps to 404."""
    import os
    # Build a db so the patch ordering doesn't matter.
    db = make_mock_db()
    # Use the real fetcher path with an empty mock to force the raise.
    with pytest.raises(TopologySelectionNotFoundError):
        await size_device(uuid.uuid4(), db=db)


@pytest.mark.smoke
async def test_candidate_idx_out_of_bounds_raises(monkeypatch):
    _patch_topology_fetch(monkeypatch, candidates=[{"name": "X"}])
    db = make_mock_db()
    with pytest.raises(CandidateIndexError):
        await size_device(SEL_ID, db=db, candidate_idx=5)


# ---------------------------------------------------------------------------
# Constraint check helper — direct unit tests
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_check_constraints_target_within_tolerance():
    spec = _spec_json()
    gaps = _check_constraints(spec, {"fc_3db": 1010.0})  # +1%, tol is 5%
    assert gaps == []


@pytest.mark.smoke
def test_check_constraints_target_out_of_tolerance():
    spec = _spec_json()
    gaps = _check_constraints(spec, {"fc_3db": 1500.0})  # +50%, tol is 5%
    assert gaps
    assert "fc_3db" in gaps[0]
    assert "out of tolerance" in gaps[0]


@pytest.mark.smoke
def test_check_constraints_max_violated():
    spec = {
        "constraints": [
            {"id": "vpp", "kind": "electrical.voltage", "max": 3.3, "unit": "V"}
        ]
    }
    gaps = _check_constraints(spec, {"vpp": 5.0})
    assert gaps
    assert "> max" in gaps[0]


@pytest.mark.smoke
def test_check_constraints_min_violated():
    spec = {
        "constraints": [
            {"id": "rin", "kind": "electrical.impedance", "min": 10000.0, "unit": "ohm"}
        ]
    }
    gaps = _check_constraints(spec, {"rin": 5000.0})
    assert gaps
    assert "< min" in gaps[0]


@pytest.mark.smoke
def test_check_constraints_required_measurable_unmeasured_is_gap():
    """An ``electrical.*`` ``required`` constraint with no matching
    measurement is a GAP — the LLM forgot the .meas line. Reporting
    convergence on an unmeasured spec would silently lie about
    verification (caught by the live integration test on first run)."""
    spec = _spec_json()
    gaps = _check_constraints(spec, {})  # measurements lacks fc_3db
    assert len(gaps) == 1
    assert "fc_3db" in gaps[0]
    assert "not measured" in gaps[0]


@pytest.mark.smoke
def test_check_constraints_skips_non_measurable_kinds():
    """cost.* / physical.* constraints aren't ngspice-observable; the
    sizing loop ignores them whether or not they have measurements."""
    spec = {
        "constraints": [
            {"id": "bom", "kind": "cost.bom_usd", "max": 5.0, "unit": "USD"},
            {"id": "area", "kind": "physical.area", "max": 100.0, "unit": "mm2"},
        ]
    }
    gaps = _check_constraints(spec, {})  # no measurements
    assert gaps == []


@pytest.mark.smoke
def test_system_prompt_includes_worked_example_and_pitfalls():
    """§17.150 — the prompt MUST carry a worked example and explicit
    pitfall callouts. The §17.147 live-test convergence depended on
    these; a future edit that drops them could silently regress
    cloud-LLM convergence rate without breaking any other test."""
    prompt = ds_mod._SYSTEM_PROMPT
    # Worked example markers.
    assert "WORKED EXAMPLE" in prompt
    assert "RC low-pass" in prompt
    # The canonical correct `meas` form.
    assert "meas ac fc_3db when vdb(out)=-3 fall=1" in prompt
    # Specific failure-mode callouts tied to actual sim_runs we saw.
    assert "PITFALL 1" in prompt and "measure limited to" in prompt
    assert "mag(v(node))=0.7071" in prompt  # PITFALL 2: wrong expr form
    assert "fall=1" in prompt and "rise=1" in prompt  # PITFALL 3
    # ngspice 44.x reference so the LLM knows the dialect.
    assert "ngspice 44.x" in prompt


@pytest.mark.smoke
def test_check_constraints_skips_unmeasured_non_required():
    """preferred / best_effort constraints with no measurement don't
    block convergence — only required ones do."""
    spec = {
        "constraints": [
            {
                "id": "nice_to_have", "kind": "electrical.gain",
                "target": 1.0, "unit": "x",
                "criticality": "preferred",
            }
        ]
    }
    gaps = _check_constraints(spec, {})
    assert gaps == []
