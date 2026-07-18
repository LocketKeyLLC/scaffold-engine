"""
Unit tests for ``app.sim.design_pipeline`` — the design_circuit
orchestrator that glues §17.144 → §17.148 into one job lifecycle.

Each stage's underlying function (``extract_spec``, ``select_topologies``,
``size_device``, ``build_report``) is mocked. We assert:
  - ``create_design_job`` writes a jobs row + links specs.job_id on
    success; writes nothing on ambiguity / extractor error.
  - ``advance_design_stage`` yields the expected SSE event sequence
    per stage and per outcome.
  - ``get_design_state`` aggregates the chain correctly.
"""
from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.sim import design_pipeline as dp
from app.sim.design_pipeline import (
    DesignBadStageError,
    DesignJobNotFoundError,
    advance_design_stage,
    create_design_job,
    get_design_state,
)
from app.sim.device_sizing import DeviceSizingResult
from app.sim.formal_verify import FormalVerifyResult
from app.sim.spec_extractor import ExtractionAmbiguity, ExtractionResult
from app.sim.topology_select import TopologyCandidate, TopologySelectionResult
from tests.conftest import make_mock_db


JOB_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SPEC_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
SEL_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
SIZING_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")
DSID = uuid.UUID("55555555-5555-5555-5555-555555555555")
FORMAL_ID = uuid.UUID("66666666-6666-6666-6666-666666666666")


# ---------------------------------------------------------------------------
# create_design_job
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_create_design_job_success_writes_job_and_links_spec(monkeypatch):
    async def fake_extract(brief, *, db, model_role=None):
        return ExtractionResult(
            ok=True,
            spec={"design": {"name": "RC LPF"}},
            spec_id=SPEC_ID,
            model_used="cloud-235b",
        )
    monkeypatch.setattr(
        "app.sim.design_pipeline.extract_spec", fake_extract
    )
    db = make_mock_db(scalar=JOB_ID)

    result = await create_design_job("Build an RC low-pass.", db=db)

    assert result.job_id == JOB_ID
    assert result.spec_id == SPEC_ID
    assert result.ambiguities == []
    assert result.errors == []
    # Two writes: INSERT job + UPDATE specs.job_id.
    assert db.execute.await_count == 2
    assert db.commit.await_count == 1


@pytest.mark.smoke
async def test_create_design_job_ambiguity_no_writes(monkeypatch):
    async def fake_extract(brief, *, db, model_role=None):
        return ExtractionResult(
            ok=False,
            ambiguities=[
                ExtractionAmbiguity(
                    field="constraints[0].target",
                    reason="fc unspecified",
                    question="Which corner frequency?",
                )
            ],
            model_used="cloud-235b",
        )
    monkeypatch.setattr(
        "app.sim.design_pipeline.extract_spec", fake_extract
    )
    db = make_mock_db()

    result = await create_design_job("Make a fast filter.", db=db)

    assert result.job_id is None
    assert result.spec_id is None
    assert len(result.ambiguities) == 1
    # No job/spec INSERT/UPDATE.
    assert db.execute.await_count == 0


@pytest.mark.smoke
async def test_create_design_job_extractor_error_no_writes(monkeypatch):
    async def fake_extract(brief, *, db, model_role=None):
        return ExtractionResult(
            ok=False,
            errors=["LLM call failed"],
            model_used="cloud-235b",
        )
    monkeypatch.setattr(
        "app.sim.design_pipeline.extract_spec", fake_extract
    )
    db = make_mock_db()

    result = await create_design_job("...", db=db)

    assert result.job_id is None
    assert result.errors == ["LLM call failed"]
    assert db.execute.await_count == 0


@pytest.mark.smoke
async def test_create_design_job_rejects_empty_brief():
    db = make_mock_db()
    with pytest.raises(ValueError):
        await create_design_job("", db=db)


# ---------------------------------------------------------------------------
# advance_design_stage
# ---------------------------------------------------------------------------

def _job_row(*, status: str = "awaiting_confirmation") -> dict:
    return {
        "id": JOB_ID,
        "status": status,
        "input_text": "Build an RC low-pass.",
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
        "job_type": "design_circuit",
    }


def _spec_row(*, confirmed: bool = True) -> dict:
    return {
        "id": SPEC_ID,
        "confirmed_at": datetime.now(timezone.utc) if confirmed else None,
    }


def _patch_chain(
    monkeypatch,
    *,
    job=None,
    spec=None,
    sel=None,
    sizing=None,
    formal=None,
    digital_sizing=None,
):
    """Mock all the DB fetchers + the per-stage workers. Patches are
    set per-test based on which sub-state the test cares about."""
    async def fake_fetch_job(db, jid):
        if job is None:
            raise DesignJobNotFoundError(f"job {jid}")
        return job

    async def fake_fetch_spec(db, jid):
        return spec

    async def fake_fetch_sel(db, sid):
        return sel

    async def fake_fetch_sizing(db, ssid):
        return sizing

    async def fake_fetch_formal(db, sid):
        return formal

    async def fake_fetch_converged_digital(db, sid):
        return digital_sizing

    async def fake_set_status(db, jid, status):
        # §17.356 — `_set_job_status` now returns bool (True on transition,
        # False if the row was already cancelled). Tests that don't care
        # about cancellation get the default "transition succeeded" reply.
        return True

    monkeypatch.setattr("app.sim.design_pipeline._fetch_design_job", fake_fetch_job)
    monkeypatch.setattr("app.sim.design_pipeline._fetch_spec_for_job", fake_fetch_spec)
    monkeypatch.setattr("app.sim.design_pipeline._fetch_latest_topology_selection", fake_fetch_sel)
    monkeypatch.setattr("app.sim.design_pipeline._fetch_latest_device_sizing", fake_fetch_sizing)
    # §17.153 — report stage now uses _fetch_latest_sizing_any_kind.
    # Patch it to the same fake so the test fixtures unify across the
    # old (device-only) and new (device+digital) lookup paths.
    monkeypatch.setattr("app.sim.design_pipeline._fetch_latest_sizing_any_kind", fake_fetch_sizing)
    # §17.414 — formal-verify stage fetchers.
    monkeypatch.setattr("app.sim.design_pipeline._fetch_latest_formal_verification", fake_fetch_formal)
    monkeypatch.setattr("app.sim.design_pipeline._fetch_latest_converged_digital_sizing", fake_fetch_converged_digital)
    monkeypatch.setattr("app.sim.design_pipeline._set_job_status", fake_set_status)


def _parse_sse(events: list[str]) -> list[tuple[str, dict]]:
    """Parse a list of SSE-formatted strings into (event_type, data) tuples."""
    out = []
    for raw in events:
        lines = raw.strip().split("\n")
        event = data = None
        for line in lines:
            if line.startswith("event:"):
                event = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data = json.loads(line[len("data:"):].strip())
        if event is not None:
            out.append((event, data))
    return out


async def _collect(gen) -> list[str]:
    return [event async for event in gen]


@pytest.mark.smoke
async def test_advance_topology_success_emits_stage_done(monkeypatch):
    candidates = [
        TopologyCandidate(
            name="RC low-pass", description="R+C",
            rationale="trivial", citations=["chunk-A"],
        ),
        TopologyCandidate(
            name="Sallen-Key LPF", description="active",
            rationale="2-pole", citations=["chunk-B"],
        ),
    ]

    async def fake_select(spec_id, *, db, **kwargs):
        return TopologySelectionResult(
            ok=True,
            selection_id=SEL_ID,
            candidates=candidates,
            rag_chunk_ids=["chunk-A", "chunk-B"],
            rag_query="analog_circuit filter",
            model_used="cloud-235b",
        )

    _patch_chain(monkeypatch, job=_job_row(), spec=_spec_row())
    monkeypatch.setattr(
        "app.sim.design_pipeline.select_topologies", fake_select
    )
    db = make_mock_db()

    events = _parse_sse(
        await _collect(advance_design_stage(JOB_ID, "topology", db=db))
    )

    kinds = [e[0] for e in events]
    assert kinds == ["stage_start", "stage_done", "done"]
    done_payload = events[1][1]
    assert done_payload["selection_id"] == str(SEL_ID)
    assert len(done_payload["candidates"]) == 2


@pytest.mark.smoke
async def test_advance_topology_stage_error_on_unconfirmed(monkeypatch):
    """Topology calls require_confirmed_spec internally; an unconfirmed
    spec surfaces as ``ok=False`` with the right error message inside
    the TopologySelectionResult."""
    async def fake_select(spec_id, *, db, **kwargs):
        return TopologySelectionResult(
            ok=False,
            errors=[f"spec {spec_id} is not confirmed; POST /specs/{spec_id}/confirm first"],
        )

    _patch_chain(monkeypatch, job=_job_row(), spec=_spec_row(confirmed=False))
    monkeypatch.setattr(
        "app.sim.design_pipeline.select_topologies", fake_select
    )
    db = make_mock_db()

    events = _parse_sse(
        await _collect(advance_design_stage(JOB_ID, "topology", db=db))
    )
    kinds = [e[0] for e in events]
    assert kinds == ["stage_start", "stage_error", "done"]
    assert events[-1][1]["ok"] is False


@pytest.mark.smoke
async def test_advance_size_success(monkeypatch):
    async def fake_size(sel_id, *, db, **kwargs):
        return DeviceSizingResult(
            ok=True,
            sizing_id=SIZING_ID,
            spec_id=SPEC_ID,
            topology_selection_id=SEL_ID,
            converged=True,
            iterations=1,
            final_measurements={"fc_3db": 998.0},
            model_used="cloud-235b",
        )

    _patch_chain(
        monkeypatch,
        job=_job_row(status="planning"),
        spec=_spec_row(),
        sel={"id": SEL_ID},
    )
    monkeypatch.setattr(
        "app.sim.design_pipeline.size_device", fake_size
    )
    db = make_mock_db()

    events = _parse_sse(
        await _collect(advance_design_stage(JOB_ID, "size", db=db))
    )
    kinds = [e[0] for e in events]
    assert kinds == ["stage_start", "stage_done", "done"]
    assert events[1][1]["converged"] is True


@pytest.mark.smoke
async def test_advance_size_no_topology_yet(monkeypatch):
    _patch_chain(
        monkeypatch,
        job=_job_row(),
        spec=_spec_row(),
        sel=None,  # no topology selection yet
    )
    db = make_mock_db()

    events = _parse_sse(
        await _collect(advance_design_stage(JOB_ID, "size", db=db))
    )
    kinds = [e[0] for e in events]
    assert kinds == ["stage_start", "stage_error", "done"]
    err = events[1][1]
    assert "stage=topology first" in err["errors"][0]


@pytest.mark.smoke
async def test_advance_report_success(monkeypatch):
    from app.sim.report import ReportDocument

    async def fake_build_report(sizing_id, *, db, **kwargs):
        return ReportDocument(
            report_schema_version="1.0.0",
            generated_at=datetime.now(timezone.utc),
            sizing_id=SIZING_ID,
            spec_id=SPEC_ID,
            topology_selection_id=SEL_ID,
            candidate_idx=0,
            converged=True,
            iterations=1,
            design_name="RC LPF",
            design_kind="analog_circuit",
            design_description=".",
            spec_schema_version="1.0.0",
            model_used="cloud-235b",
        )

    _patch_chain(
        monkeypatch,
        job=_job_row(status="executing"),
        spec=_spec_row(),
        sel={"id": SEL_ID},
        sizing={"id": SIZING_ID, "converged": True},
    )
    monkeypatch.setattr(
        "app.sim.design_pipeline.build_report", fake_build_report
    )
    db = make_mock_db()

    events = _parse_sse(
        await _collect(advance_design_stage(JOB_ID, "report", db=db))
    )
    kinds = [e[0] for e in events]
    assert kinds == ["stage_start", "stage_done", "done"]
    assert "# RC LPF" in events[1][1]["markdown"]
    assert events[-1][1]["ok"] is True


@pytest.mark.smoke
async def test_advance_verify_success(monkeypatch):
    """§17.414 — verify stage against a converged digital sizing emits
    stage_done with the formal verdict."""
    async def fake_verify(digital_sizing_id, *, db, **kwargs):
        assert digital_sizing_id == DSID
        return FormalVerifyResult(
            ok=True,
            formal_verification_id=FORMAL_ID,
            spec_id=SPEC_ID,
            topology_selection_id=SEL_ID,
            digital_sizing_id=DSID,
            converged=True,
            verdict="PASS",
            depth_reached=20,
            iterations=1,
        )

    _patch_chain(
        monkeypatch,
        job=_job_row(status="executing"),
        spec=_spec_row(),
        sel={"id": SEL_ID},
        digital_sizing={"id": DSID, "converged": True},
    )
    monkeypatch.setattr("app.sim.design_pipeline.verify_design", fake_verify)
    db = make_mock_db()

    events = _parse_sse(
        await _collect(advance_design_stage(JOB_ID, "verify", db=db))
    )
    kinds = [e[0] for e in events]
    assert kinds == ["stage_start", "stage_done", "done"]
    payload = events[1][1]
    assert payload["verdict"] == "PASS"
    assert payload["converged"] is True
    assert payload["formal_verification_id"] == str(FORMAL_ID)
    assert events[-1][1]["ok"] is True


@pytest.mark.smoke
async def test_advance_verify_no_converged_digital_sizing(monkeypatch):
    """No converged digital sizing → stage_error (formal verify is
    digital-only and needs a Verilator-proven DUT to start from)."""
    _patch_chain(
        monkeypatch,
        job=_job_row(status="executing"),
        spec=_spec_row(),
        sel={"id": SEL_ID},
        digital_sizing=None,  # nothing converged
    )
    db = make_mock_db()

    events = _parse_sse(
        await _collect(advance_design_stage(JOB_ID, "verify", db=db))
    )
    kinds = [e[0] for e in events]
    assert kinds == ["stage_start", "stage_error", "done"]
    assert "digital_logic" in events[1][1]["errors"][0]


@pytest.mark.smoke
async def test_advance_verify_non_pass_leaves_done_false(monkeypatch):
    """A non-PASS verdict yields stage_done + done(ok=False); the job is
    left in 'executing' (no failed/completed transition)."""
    async def fake_verify(digital_sizing_id, *, db, **kwargs):
        return FormalVerifyResult(
            ok=False,
            formal_verification_id=FORMAL_ID,
            converged=False,
            verdict="FAIL",
            depth_reached=3,
            iterations=3,
        )

    _patch_chain(
        monkeypatch,
        job=_job_row(status="executing"),
        spec=_spec_row(),
        sel={"id": SEL_ID},
        digital_sizing={"id": DSID, "converged": True},
    )
    monkeypatch.setattr("app.sim.design_pipeline.verify_design", fake_verify)
    db = make_mock_db()

    events = _parse_sse(
        await _collect(advance_design_stage(JOB_ID, "verify", db=db))
    )
    kinds = [e[0] for e in events]
    assert kinds == ["stage_start", "stage_done", "done"]
    assert events[1][1]["verdict"] == "FAIL"
    assert events[-1][1]["ok"] is False


@pytest.mark.smoke
async def test_advance_verify_honors_mid_stage_cancellation(monkeypatch):
    """§17.356 post-await check applies to the verify stage too: a cancel
    landing during verify_design yields 'cancelled', not stage_done."""
    async def fake_verify(digital_sizing_id, *, db, **kwargs):
        return FormalVerifyResult(ok=True, formal_verification_id=FORMAL_ID,
                                  converged=True, verdict="PASS")

    async def fake_was_cancelled(db, jid):
        return True

    _patch_chain(
        monkeypatch,
        job=_job_row(status="executing"),
        spec=_spec_row(),
        sel={"id": SEL_ID},
        digital_sizing={"id": DSID, "converged": True},
    )
    monkeypatch.setattr("app.sim.design_pipeline.verify_design", fake_verify)
    monkeypatch.setattr("app.sim.design_pipeline._job_was_cancelled", fake_was_cancelled)
    db = make_mock_db()

    events = _parse_sse(
        await _collect(advance_design_stage(JOB_ID, "verify", db=db))
    )
    kinds = [e[0] for e in events]
    assert "stage_done" not in kinds
    assert "cancelled" in kinds
    assert events[-1][1]["ok"] is False


@pytest.mark.smoke
async def test_advance_unknown_stage_emits_stage_error(monkeypatch):
    _patch_chain(monkeypatch, job=_job_row(), spec=_spec_row())
    db = make_mock_db()

    events = _parse_sse(
        await _collect(advance_design_stage(JOB_ID, "bogus", db=db))
    )
    kinds = [e[0] for e in events]
    assert kinds == ["stage_error", "done"]


@pytest.mark.smoke
async def test_advance_missing_job_emits_stage_error(monkeypatch):
    _patch_chain(monkeypatch, job=None)
    db = make_mock_db()

    events = _parse_sse(
        await _collect(advance_design_stage(JOB_ID, "topology", db=db))
    )
    kinds = [e[0] for e in events]
    assert kinds == ["stage_error", "done"]


# ---------------------------------------------------------------------------
# get_design_state
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_get_design_state_full_chain(monkeypatch):
    _patch_chain(
        monkeypatch,
        job=_job_row(status="completed"),
        spec=_spec_row(),
        sel={"id": SEL_ID},
        sizing={"id": SIZING_ID, "converged": True},
        formal={"id": FORMAL_ID, "verdict": "PASS", "converged": True},
    )
    db = make_mock_db()
    state = await get_design_state(JOB_ID, db=db)
    assert state.status == "completed"
    assert state.spec_id == SPEC_ID
    assert state.topology_selection_id == SEL_ID
    assert state.device_sizing_id == SIZING_ID
    assert state.device_sizing_converged is True
    assert state.formal_verification_id == FORMAL_ID
    assert state.formal_verdict == "PASS"


@pytest.mark.smoke
async def test_get_design_state_reports_digital_sizing(monkeypatch):
    """§17.600 — digital designs store sizing in digital_sizings; get_design_state
    must union both tables. Was device_sizings-only, so every digital design
    reported device_sizing_id=None even with a converged sizing (+ formal PASS)."""
    _patch_chain(
        monkeypatch,
        job=_job_row(status="completed"),
        spec=_spec_row(),
        sel={"id": SEL_ID},
        formal={"id": FORMAL_ID, "verdict": "PASS", "converged": True},
    )

    # Analog-only lookup finds nothing; the any-kind union returns the digital
    # sizing. Override the unified fakes _patch_chain set.
    async def _device_only(db, sid):
        return None

    async def _any_kind(db, sid):
        return {"id": SIZING_ID, "converged": True, "kind": "digital"}

    monkeypatch.setattr("app.sim.design_pipeline._fetch_latest_device_sizing", _device_only)
    monkeypatch.setattr("app.sim.design_pipeline._fetch_latest_sizing_any_kind", _any_kind)
    db = make_mock_db()
    state = await get_design_state(JOB_ID, db=db)
    assert state.device_sizing_id == SIZING_ID
    assert state.device_sizing_converged is True
    assert state.formal_verdict == "PASS"


@pytest.mark.smoke
async def test_get_design_state_only_extract_done(monkeypatch):
    _patch_chain(
        monkeypatch,
        job=_job_row(),
        spec=_spec_row(confirmed=False),
        sel=None,
        sizing=None,
    )
    db = make_mock_db()
    state = await get_design_state(JOB_ID, db=db)
    assert state.spec_id == SPEC_ID
    assert state.spec_confirmed_at is None
    assert state.topology_selection_id is None
    assert state.device_sizing_id is None
    assert state.device_sizing_converged is None


@pytest.mark.smoke
async def test_get_design_state_404_on_missing(monkeypatch):
    _patch_chain(monkeypatch, job=None)
    db = make_mock_db()
    with pytest.raises(DesignJobNotFoundError):
        await get_design_state(JOB_ID, db=db)


# ---------------------------------------------------------------------------
# §17.356 — cancellation invariants
# ---------------------------------------------------------------------------


@pytest.mark.smoke
async def test_advance_refuses_to_advance_already_cancelled_job(monkeypatch):
    """§17.356 precondition: a job already in 'cancelled' status when
    advance_design_stage starts emits 'cancelled' + done(ok=False) and
    never calls into the stage worker."""
    select_calls = []

    async def fake_select(spec_id, *, db, **kwargs):
        select_calls.append(spec_id)
        return TopologySelectionResult(ok=True, selection_id=SEL_ID, candidates=[])

    _patch_chain(monkeypatch, job=_job_row(status="cancelled"), spec=_spec_row())
    monkeypatch.setattr("app.sim.design_pipeline.select_topologies", fake_select)
    db = make_mock_db()

    events = _parse_sse(
        await _collect(advance_design_stage(JOB_ID, "topology", db=db))
    )
    kinds = [e[0] for e in events]
    assert kinds == ["cancelled", "done"]
    assert events[-1][1]["ok"] is False
    # Critical: stage worker MUST NOT have been invoked.
    assert select_calls == []


@pytest.mark.smoke
async def test_advance_topology_honors_mid_stage_cancellation(monkeypatch):
    """§17.356 post-await check: cancel landing during select_topologies
    causes 'cancelled' to be emitted instead of stage_done — and
    stage_done MUST NOT appear in the SSE stream."""
    async def fake_select(spec_id, *, db, **kwargs):
        return TopologySelectionResult(
            ok=True, selection_id=SEL_ID,
            candidates=[TopologyCandidate(
                name="x", description="y", rationale="z", citations=["c"],
            )],
        )

    # Job starts non-cancelled, then a mid-stage probe sees 'cancelled'.
    _patch_chain(monkeypatch, job=_job_row(status="awaiting_confirmation"), spec=_spec_row())
    monkeypatch.setattr("app.sim.design_pipeline.select_topologies", fake_select)

    # Precondition check uses job["status"] from _fetch_design_job (which
    # _patch_chain mocked to "awaiting_confirmation"), so it passes.
    # The post-await check calls _job_was_cancelled — patch it to True
    # so the stage handler honors a cancel that landed during the LLM call.
    async def fake_was_cancelled(db, jid):
        return True

    monkeypatch.setattr(
        "app.sim.design_pipeline._job_was_cancelled", fake_was_cancelled
    )
    db = make_mock_db()

    events = _parse_sse(
        await _collect(advance_design_stage(JOB_ID, "topology", db=db))
    )
    kinds = [e[0] for e in events]
    assert "stage_done" not in kinds
    assert "cancelled" in kinds
    assert events[-1][0] == "done"
    assert events[-1][1]["ok"] is False


@pytest.mark.smoke
async def test_set_job_status_refuses_to_overwrite_cancelled(monkeypatch):
    """§17.356 sticky-cancel invariant: `_set_job_status` returns False
    when the row was already cancelled, so the WHERE-guarded UPDATE
    affects 0 rows and the caller can detect the race."""
    from app.sim.design_pipeline import _set_job_status

    # Mock rowcount=0 to simulate the WHERE status != 'cancelled' guard
    # rejecting the UPDATE (i.e. the row IS cancelled).
    db_rejected = make_mock_db(rowcount=0)
    transitioned = await _set_job_status(db_rejected, JOB_ID, "completed")
    assert transitioned is False

    # Mock rowcount=1 simulates a successful transition.
    db_ok = make_mock_db(rowcount=1)
    transitioned = await _set_job_status(db_ok, JOB_ID, "completed")
    assert transitioned is True
