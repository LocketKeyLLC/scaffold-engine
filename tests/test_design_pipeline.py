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
from app.sim.spec_extractor import ExtractionAmbiguity, ExtractionResult
from app.sim.topology_select import TopologyCandidate, TopologySelectionResult
from tests.conftest import make_mock_db


JOB_ID = uuid.UUID("11111111-1111-1111-1111-111111111111")
SPEC_ID = uuid.UUID("22222222-2222-2222-2222-222222222222")
SEL_ID = uuid.UUID("33333333-3333-3333-3333-333333333333")
SIZING_ID = uuid.UUID("44444444-4444-4444-4444-444444444444")


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
):
    """Mock all four DB fetchers + the per-stage workers. Patches are
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

    async def fake_set_status(db, jid, status):
        return None

    monkeypatch.setattr("app.sim.design_pipeline._fetch_design_job", fake_fetch_job)
    monkeypatch.setattr("app.sim.design_pipeline._fetch_spec_for_job", fake_fetch_spec)
    monkeypatch.setattr("app.sim.design_pipeline._fetch_latest_topology_selection", fake_fetch_sel)
    monkeypatch.setattr("app.sim.design_pipeline._fetch_latest_device_sizing", fake_fetch_sizing)
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
    )
    db = make_mock_db()
    state = await get_design_state(JOB_ID, db=db)
    assert state.status == "completed"
    assert state.spec_id == SPEC_ID
    assert state.topology_selection_id == SEL_ID
    assert state.device_sizing_id == SIZING_ID
    assert state.device_sizing_converged is True


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
