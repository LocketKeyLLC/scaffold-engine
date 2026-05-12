"""
Unit tests for ``app.sim.topology_select`` — first reasoning stage.

RAG and LLM are mocked; the only contract that matters is that
``select_topologies`` enforces the §17.146 citation invariant and
never persists a row on any failure path.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from app.providers.base import ModelResponse
from app.sim import topology_select as ts_mod
from app.sim.spec_store import SpecRow
from app.sim.topology_select import (
    TopologyCandidate,
    TopologySelectionResult,
    select_topologies,
)
from tests.conftest import make_mock_db


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def spec_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def confirmed_spec_row(spec_id) -> SpecRow:
    return SpecRow(
        id=spec_id,
        job_id=None,
        schema_version="1.0.0",
        spec_json={
            "schema_version": "1.0.0",
            "design": {
                "name": "RC LPF",
                "kind": "analog_circuit",
                "description": "First-order RC low-pass.",
            },
            "constraints": [
                {
                    "id": "fc_3db",
                    "kind": "electrical.frequency",
                    "description": "Corner.",
                    "target": 1000.0,
                    "unit": "Hz",
                    "criticality": "required",
                }
            ],
        },
        spec_sha256="abc123",
        confirmed_by="api_key",
        confirmed_at=datetime.now(timezone.utc),
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def unconfirmed_spec_row(confirmed_spec_row) -> SpecRow:
    return SpecRow(
        id=confirmed_spec_row.id,
        job_id=confirmed_spec_row.job_id,
        schema_version=confirmed_spec_row.schema_version,
        spec_json=confirmed_spec_row.spec_json,
        spec_sha256=confirmed_spec_row.spec_sha256,
        confirmed_by=None,
        confirmed_at=None,
        created_at=confirmed_spec_row.created_at,
    )


def _patch_require_confirmed(monkeypatch, spec_row):
    """Bypass the spec_store DB read; return our canned SpecRow."""
    async def _fake(db, sid):
        from app.sim.spec_store import SpecNotConfirmedError
        if not spec_row.is_confirmed:
            raise SpecNotConfirmedError(spec_row.id)
        return spec_row
    monkeypatch.setattr(
        "app.sim.topology_select.require_confirmed_spec", _fake
    )


def _patch_rag(monkeypatch, *, status: str = "ok", results=None, error=""):
    async def _fake(query, *, domain=None, top_k=8):
        return {
            "status": status,
            "error": error,
            "results": results or [],
            "metadata": {},
        }
    monkeypatch.setattr("app.sim.topology_select.query_rag", _fake)


def _patch_chat(monkeypatch, text_payload: str, *, success: bool = True, error=None):
    resp = ModelResponse(
        text=text_payload,
        model="qwen3-vl:235b-instruct-cloud",
        success=success,
        error=error,
    )
    monkeypatch.setattr(
        "app.sim.topology_select.model_router.chat",
        AsyncMock(return_value=resp),
    )


def _patch_insert(monkeypatch, returned_id: uuid.UUID | None = None):
    """Stub _insert_selection so we don't need to mock a DB
    INSERT-RETURNING; ``call_count`` is what the tests assert on."""
    async def _fake(db, **kwargs):
        return returned_id or uuid.uuid4()
    mock = AsyncMock(side_effect=_fake)
    monkeypatch.setattr("app.sim.topology_select._insert_selection", mock)
    return mock


def _three_chunks() -> list[dict]:
    return [
        {"entry_id": "chunk-A", "title": "Sallen-Key", "content": "Active LPF…"},
        {"entry_id": "chunk-B", "title": "MFB",        "content": "Multiple-feedback…"},
        {"entry_id": "chunk-C", "title": "RC ladder",  "content": "Passive cascade…"},
    ]


def _valid_llm_body() -> dict:
    return {
        "candidates": [
            {
                "name": "Sallen-Key low-pass",
                "description": "Active 2-pole LPF.",
                "rationale": "Meets 1 kHz corner with op-amp.",
                "citations": ["chunk-A"],
            },
            {
                "name": "RC passive ladder",
                "description": "Single-pole cascade.",
                "rationale": "Simplest passive realization.",
                "citations": ["chunk-C"],
            },
        ]
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_select_topologies_happy_path_persists_row(
    monkeypatch, confirmed_spec_row, spec_id,
):
    _patch_require_confirmed(monkeypatch, confirmed_spec_row)
    _patch_rag(monkeypatch, results=_three_chunks())
    _patch_chat(monkeypatch, json.dumps(_valid_llm_body()))
    insert_mock = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await select_topologies(spec_id, db=db)

    assert result.ok is True
    assert result.selection_id is not None
    assert len(result.candidates) == 2
    assert {"chunk-A", "chunk-B", "chunk-C"} == set(result.rag_chunk_ids)
    assert insert_mock.await_count == 1
    # Insert payload carries citations verbatim.
    insert_kwargs = insert_mock.await_args.kwargs
    persisted_cites = [c.citations for c in insert_kwargs["candidates"]]
    assert ["chunk-A"] in persisted_cites
    assert ["chunk-C"] in persisted_cites


# ---------------------------------------------------------------------------
# Failure paths — every one must skip the INSERT
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_unconfirmed_spec_surfaces_as_ok_false(
    monkeypatch, unconfirmed_spec_row, spec_id,
):
    _patch_require_confirmed(monkeypatch, unconfirmed_spec_row)
    insert_mock = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await select_topologies(spec_id, db=db)

    assert result.ok is False
    assert any("not confirmed" in e for e in result.errors)
    assert insert_mock.await_count == 0


@pytest.mark.smoke
async def test_rag_empty_corpus_no_row(
    monkeypatch, confirmed_spec_row, spec_id,
):
    _patch_require_confirmed(monkeypatch, confirmed_spec_row)
    _patch_rag(monkeypatch, results=[])
    _patch_chat(monkeypatch, json.dumps(_valid_llm_body()))  # never reached
    insert_mock = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await select_topologies(spec_id, db=db)

    assert result.ok is False
    assert any("0 chunks" in e for e in result.errors)
    assert insert_mock.await_count == 0


@pytest.mark.smoke
async def test_rag_error_status_no_row(
    monkeypatch, confirmed_spec_row, spec_id,
):
    _patch_require_confirmed(monkeypatch, confirmed_spec_row)
    _patch_rag(monkeypatch, status="error", error="collection_unavailable")
    insert_mock = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await select_topologies(spec_id, db=db)

    assert result.ok is False
    assert any("RAG retrieval failed" in e for e in result.errors)
    assert insert_mock.await_count == 0


@pytest.mark.smoke
async def test_llm_failure_no_row(
    monkeypatch, confirmed_spec_row, spec_id,
):
    _patch_require_confirmed(monkeypatch, confirmed_spec_row)
    _patch_rag(monkeypatch, results=_three_chunks())
    _patch_chat(monkeypatch, "", success=False, error="model unreachable")
    insert_mock = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await select_topologies(spec_id, db=db)

    assert result.ok is False
    assert any("LLM call failed" in e for e in result.errors)
    assert insert_mock.await_count == 0


@pytest.mark.smoke
async def test_llm_unparseable_no_row(
    monkeypatch, confirmed_spec_row, spec_id,
):
    _patch_require_confirmed(monkeypatch, confirmed_spec_row)
    _patch_rag(monkeypatch, results=_three_chunks())
    _patch_chat(monkeypatch, "this is not JSON")
    insert_mock = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await select_topologies(spec_id, db=db)

    assert result.ok is False
    assert any("did not parse" in e for e in result.errors)
    assert insert_mock.await_count == 0


# ---------------------------------------------------------------------------
# The verifiability invariant — hard-reject hallucinated citations.
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_hallucinated_citation_rejects_whole_step(
    monkeypatch, confirmed_spec_row, spec_id,
):
    _patch_require_confirmed(monkeypatch, confirmed_spec_row)
    _patch_rag(monkeypatch, results=_three_chunks())
    body = _valid_llm_body()
    # Add an extra candidate that cites a chunk not in the retrieval set.
    body["candidates"].append({
        "name": "Fabricated topology",
        "description": "...",
        "rationale": "...",
        "citations": ["chunk-Z-DOES-NOT-EXIST"],
    })
    _patch_chat(monkeypatch, json.dumps(body))
    insert_mock = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await select_topologies(spec_id, db=db)

    assert result.ok is False
    assert any("hallucinated citation" in e for e in result.errors)
    assert any("chunk-Z-DOES-NOT-EXIST" in e for e in result.errors)
    # The whole step fails — no partial persistence even though two
    # candidates had valid citations.
    assert insert_mock.await_count == 0


@pytest.mark.smoke
async def test_candidate_with_no_citations_rejected(
    monkeypatch, confirmed_spec_row, spec_id,
):
    _patch_require_confirmed(monkeypatch, confirmed_spec_row)
    _patch_rag(monkeypatch, results=_three_chunks())
    body = {
        "candidates": [
            {
                "name": "X",
                "description": "X",
                "rationale": "X",
                "citations": [],  # empty
            },
            {
                "name": "Y",
                "description": "Y",
                "rationale": "Y",
                "citations": ["chunk-A"],
            },
        ]
    }
    _patch_chat(monkeypatch, json.dumps(body))
    insert_mock = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await select_topologies(spec_id, db=db)

    assert result.ok is False
    assert any("no citations" in e for e in result.errors)
    assert insert_mock.await_count == 0


# ---------------------------------------------------------------------------
# Cardinality bounds — 2-4 candidates.
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_too_few_candidates_rejected(
    monkeypatch, confirmed_spec_row, spec_id,
):
    _patch_require_confirmed(monkeypatch, confirmed_spec_row)
    _patch_rag(monkeypatch, results=_three_chunks())
    body = {
        "candidates": [
            {
                "name": "X",
                "description": "X",
                "rationale": "X",
                "citations": ["chunk-A"],
            }
        ]
    }
    _patch_chat(monkeypatch, json.dumps(body))
    insert_mock = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await select_topologies(spec_id, db=db)

    assert result.ok is False
    assert any("1 candidates" in e for e in result.errors)
    assert insert_mock.await_count == 0


@pytest.mark.smoke
async def test_too_many_candidates_rejected(
    monkeypatch, confirmed_spec_row, spec_id,
):
    _patch_require_confirmed(monkeypatch, confirmed_spec_row)
    _patch_rag(monkeypatch, results=_three_chunks())
    body = {
        "candidates": [
            {
                "name": f"X{i}",
                "description": "X",
                "rationale": "X",
                "citations": ["chunk-A"],
            }
            for i in range(5)
        ]
    }
    _patch_chat(monkeypatch, json.dumps(body))
    insert_mock = _patch_insert(monkeypatch)
    db = make_mock_db()

    result = await select_topologies(spec_id, db=db)

    assert result.ok is False
    assert any("5 candidates" in e for e in result.errors)
    assert insert_mock.await_count == 0


# ---------------------------------------------------------------------------
# Helper-level tests
# ---------------------------------------------------------------------------

@pytest.mark.smoke
def test_build_rag_query_excludes_numeric_values():
    """The query should be about topology *families*, not parameter
    values — including specific corner frequencies would drag the
    retrieval toward calculator pages instead of design references."""
    spec = {
        "design": {
            "name": "RC low-pass",
            "kind": "analog_circuit",
            "description": "First-order low-pass for audio.",
        },
        "constraints": [
            {"kind": "electrical.frequency", "target": 1000.0},
            {"kind": "electrical.voltage", "max": 3.3},
        ],
    }
    q = ts_mod._build_rag_query(spec)
    assert "analog_circuit" in q
    assert "electrical.frequency" in q
    assert "electrical.voltage" in q
    # No raw numeric values should leak into the retrieval query.
    assert "1000" not in q
    assert "3.3" not in q


@pytest.mark.smoke
def test_validate_citations_returns_hallucinated():
    cands = [
        TopologyCandidate(
            name="A", description=".", rationale=".",
            citations=["good", "bad"],
        )
    ]
    out = ts_mod._validate_citations(cands, {"good"})
    assert "bad" in out


@pytest.mark.smoke
def test_validate_citations_empty_citation_list_flags():
    cands = [TopologyCandidate(name="X", description=".", rationale=".", citations=[])]
    out = ts_mod._validate_citations(cands, {"any"})
    assert any("no citations" in s for s in out)
