"""Integration tests: refine_idea against real Postgres.

Mock tests cover logic. These verify column shape, JSON serialization,
and status-transition behavior against the actual schema — catches
regressions where a column rename or constraint change would silently
pass the mocks.

Sprint X.11 + X.12 — refine_idea now uses model_router.tool_call. The
fixtures here mock that call and pass the brief via
``tool_calls[0].arguments``; pre-X.11 fixtures patched
``model_router.generate`` and supplied JSON text on ``resp.text``.
"""
from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import text

from app.modules.idea_refinement import refine_idea


pytestmark = pytest.mark.asyncio


def _fake_tool_call_resp(args: dict | None = None, *, success: bool = True,
                         error: str | None = None) -> SimpleNamespace:
    """Mimic model_router.tool_call's ModelResponse — only the fields
    refine_idea reads. ``args=None`` produces the empty tool_calls list
    used by the fail-closed path."""
    if args is None or not success:
        tool_calls = []
    else:
        tool_calls = [SimpleNamespace(arguments=args)]
    return SimpleNamespace(
        text="",
        success=success,
        error=error,
        model="test-model",
        total_duration_ms=5,
        tool_calls=tool_calls,
    )


@pytest.fixture
def stub_llm_ok():
    brief = {
        "title": "Pilot widget",
        "description": "A small widget that demonstrates the pipeline.",
        "domain": "eng",
        "goals": ["Run end-to-end"],
        "constraints": [],
        "inputs_available": [],
        "outputs_expected": ["Working code"],
        "complexity": "low",
        "ambiguities": [],
    }
    with patch("app.modules.idea_refinement.model_router.tool_call",
               new=AsyncMock(return_value=_fake_tool_call_resp(brief))):
        yield brief


async def test_refine_idea_inserts_job_and_persists_brief(db_session, stub_llm_ok, track_job):
    """Happy path: refined_brief column receives valid JSONB; status transitions
    to awaiting_confirmation; title is set from the LLM brief."""
    result = await refine_idea(
        "Build a small widget pipeline.",
        db_session,
        target_status="awaiting_confirmation",
    )

    assert result["status"] == "awaiting_confirmation"
    job_id = track_job(result["job_id"])

    row = (await db_session.execute(
        text("SELECT title, status, refined_brief FROM jobs WHERE id = :id"),
        {"id": job_id},
    )).mappings().first()
    assert row is not None
    assert row["status"] == "awaiting_confirmation"
    assert row["title"] == "Pilot widget"
    # refined_brief comes back as a Python dict (JSONB-decoded) on Postgres.
    brief = row["refined_brief"]
    if isinstance(brief, str):
        brief = json.loads(brief)
    assert brief["description"].startswith("A small widget")


async def test_refine_idea_failure_marks_job_failed(db_session, track_job):
    """LLM error path: job moves to 'failed' with truncated error_summary."""
    bad = _fake_tool_call_resp(success=False, error="ollama timeout")
    with patch("app.modules.idea_refinement.model_router.tool_call",
               new=AsyncMock(return_value=bad)):
        result = await refine_idea("anything", db_session)

    assert result["status"] == "failed"
    job_id = track_job(result["job_id"])
    row = (await db_session.execute(
        text("SELECT status, error_summary FROM jobs WHERE id = :id"),
        {"id": job_id},
    )).mappings().first()
    assert row["status"] == "failed"
    assert row["error_summary"].startswith("LLM refinement failed: ollama timeout")


async def test_refine_idea_invalid_domain_raises_before_insert(db_session):
    """Invalid domain override raises ValueError before any DB write happens."""
    sentinel = "integration-domain-test-" + str(__import__("uuid").uuid4())
    with pytest.raises(ValueError):
        await refine_idea(sentinel, db_session, domain="not-a-domain")
    cnt = (await db_session.execute(
        text("SELECT COUNT(*) FROM jobs WHERE input_text = :s"),
        {"s": sentinel},
    )).scalar_one()
    assert cnt == 0
