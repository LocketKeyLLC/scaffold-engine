"""§17.569 — grounding gate (_maybe_grounding_gate) for synthesized deliverables.

Flag-only, default-on, fail-soft. Patches score_faithfulness (lazily imported
inside the gate, so patch the source app.modules.faithfulness) + async_session
(also lazy, patch app.database).
"""
from unittest.mock import AsyncMock

import pytest

import app.database as dbm
import app.modules.cove as cov
import app.modules.faithfulness as fa
from app.config import settings
from app.modules import execution_compile as ec


class _DummyMDB:
    def __init__(self):
        self.execute = AsyncMock()
        self.commit = AsyncMock()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


@pytest.fixture
def mdb(monkeypatch):
    m = _DummyMDB()
    monkeypatch.setattr(dbm, "async_session", lambda: m)
    return m


@pytest.fixture(autouse=True)
def _gate_defaults(monkeypatch):
    monkeypatch.setattr(settings, "grounding_gate_enabled", True)
    monkeypatch.setattr(settings, "grounding_min_score", 0.7)
    # §17.569 flag-tests assume no correction; the §17.570 tests opt back in.
    monkeypatch.setattr(settings, "grounding_correct_enabled", False)


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_low_grounding_flags_and_records(monkeypatch, mdb):
    monkeypatch.setattr(fa, "score_faithfulness", AsyncMock(return_value={
        "score": 0.4, "supported": 2, "total": 5,
        "unsupported_claims": ["X is true", "Y happened"],
    }))
    out = await ec._maybe_grounding_gate("job-1", "deliverable body", "the source work")
    assert "Grounding check" in out and "40%" in out
    assert "X is true" in out
    assert out.endswith("deliverable body")
    mdb.execute.assert_awaited()              # score recorded to jobs.metadata


@pytest.mark.asyncio
async def test_high_grounding_no_banner_but_records(monkeypatch, mdb):
    monkeypatch.setattr(fa, "score_faithfulness", AsyncMock(return_value={
        "score": 0.9, "supported": 9, "total": 10, "unsupported_claims": [],
    }))
    out = await ec._maybe_grounding_gate("job-1", "body", "ev")
    assert out == "body"                       # no banner above threshold
    mdb.execute.assert_awaited()               # still records the score


@pytest.mark.asyncio
async def test_none_is_noop_no_db_write(monkeypatch, mdb):
    monkeypatch.setattr(fa, "score_faithfulness", AsyncMock(return_value=None))
    out = await ec._maybe_grounding_gate("job-1", "body", "ev")
    assert out == "body"                       # fail-soft, unchanged
    mdb.execute.assert_not_awaited()           # no write when not scored


@pytest.mark.asyncio
async def test_disabled_skips_scoring(monkeypatch, mdb):
    monkeypatch.setattr(settings, "grounding_gate_enabled", False)
    sf = AsyncMock(return_value={"score": 0.1})
    monkeypatch.setattr(fa, "score_faithfulness", sf)
    out = await ec._maybe_grounding_gate("job-1", "body", "ev")
    assert out == "body"
    sf.assert_not_awaited()                     # gate off → scorer never called


@pytest.mark.asyncio
async def test_threshold_boundary(monkeypatch, mdb):
    monkeypatch.setattr(fa, "score_faithfulness", AsyncMock(return_value={
        "score": 0.7, "supported": 7, "total": 10, "unsupported_claims": []}))
    assert await ec._maybe_grounding_gate("j", "b", "e") == "b"   # 0.7 not < 0.7
    monkeypatch.setattr(fa, "score_faithfulness", AsyncMock(return_value={
        "score": 0.69, "supported": 69, "total": 100, "unsupported_claims": ["z"]}))
    assert "Grounding check" in await ec._maybe_grounding_gate("j", "b", "e")  # 0.69 < 0.7


@pytest.mark.asyncio
async def test_verbatim_synthesis_skip_never_reaches_gate(monkeypatch):
    """CodeGen/Shell skip synthesis (verbatim) → synthesized is None → the gate
    is never invoked (faithfulness on code is meaningless)."""
    sf = AsyncMock()
    monkeypatch.setattr(fa, "score_faithfulness", sf)
    monkeypatch.setattr(ec, "_resolve_synthesis_enabled", AsyncMock(return_value=True))
    monkeypatch.setattr(ec, "_synthesize_compiled_output", AsyncMock(return_value=None))
    txt, was = await ec._maybe_synthesize(
        job_id="j", heuristic="code body", strategy="s",
        source_tool="CodeGen", db=AsyncMock(),
    )
    assert was is False and txt == "code body"
    sf.assert_not_awaited()


# ---- §17.570 Layer A — deliverable CoVe correction ----

@pytest.mark.asyncio
async def test_correction_lifts_above_threshold_no_banner(monkeypatch, mdb):
    """Low → CoVe revises → re-score clears threshold → corrected text, no banner."""
    monkeypatch.setattr(settings, "grounding_correct_enabled", True)
    # 1st score low, 2nd score (re-score of revised) high.
    monkeypatch.setattr(fa, "score_faithfulness", AsyncMock(side_effect=[
        {"score": 0.4, "supported": 2, "total": 5, "unsupported_claims": ["bad"]},
        {"score": 0.9, "supported": 9, "total": 10, "unsupported_claims": []},
    ]))
    cove = AsyncMock(return_value={"revised": "grounded body", "changed": True, "questions": ["q"]})
    monkeypatch.setattr(cov, "cove_revise", cove)
    out = await ec._maybe_grounding_gate("job-1", "drifty body", "evidence")
    assert out == "grounded body"          # corrected text adopted
    assert "Grounding check" not in out    # re-score cleared → no banner
    cove.assert_awaited_once()
    mdb.execute.assert_awaited()           # metadata recorded (corrected:True)


@pytest.mark.asyncio
async def test_correction_still_low_banners_with_note(monkeypatch, mdb):
    """Low → CoVe revises but re-score still low → corrected text + banner noting it."""
    monkeypatch.setattr(settings, "grounding_correct_enabled", True)
    monkeypatch.setattr(fa, "score_faithfulness", AsyncMock(side_effect=[
        {"score": 0.3, "supported": 1, "total": 4, "unsupported_claims": ["a", "b"]},
        {"score": 0.5, "supported": 2, "total": 4, "unsupported_claims": ["b"]},
    ]))
    monkeypatch.setattr(cov, "cove_revise", AsyncMock(
        return_value={"revised": "less drifty", "changed": True, "questions": []}))
    out = await ec._maybe_grounding_gate("job-1", "drifty", "evidence")
    assert "Grounding check" in out and "auto-revision was attempted" in out
    assert out.endswith("less drifty")     # corrected text retained under the banner


@pytest.mark.asyncio
async def test_correct_disabled_is_flag_only(monkeypatch, mdb):
    """grounding_correct_enabled=False → no CoVe call, original flag behavior."""
    monkeypatch.setattr(settings, "grounding_correct_enabled", False)
    monkeypatch.setattr(fa, "score_faithfulness", AsyncMock(return_value={
        "score": 0.4, "supported": 2, "total": 5, "unsupported_claims": ["x"]}))
    cove = AsyncMock()
    monkeypatch.setattr(cov, "cove_revise", cove)
    out = await ec._maybe_grounding_gate("job-1", "body", "evidence")
    assert "Grounding check" in out and "auto-revision" not in out
    cove.assert_not_awaited()
