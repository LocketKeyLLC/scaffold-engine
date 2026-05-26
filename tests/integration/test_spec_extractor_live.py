"""
Live-model integration test for ``app.sim.spec_extractor``.

Hits the real ``model_router`` against the configured
``spec_extractor_model_role`` (default ``model_general`` — the
cloud-routed 235b on this host). Verifies that an unambiguous brief
round-trips end-to-end: LLM → JSON parse → schema validation →
``specs`` INSERT → row retrievable by id.

Skipped automatically when the configured model is unreachable so a
CI-without-Ollama doesn't fail. The mocked-LLM suite in
``tests/test_spec_extractor.py`` covers all behavioural surface; this
test only catches prompt-vs-real-LLM regressions.
"""
from __future__ import annotations

import json
import os

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.sim.spec import validate_spec
from app.sim.spec_extractor import extract_spec
from app.utils import http_clients

UNAMBIGUOUS_BRIEF = (
    "I need an RC low-pass filter. The -3 dB corner frequency should "
    "be 1000 Hz with ±5% tolerance. Input swing is 0-5 V. Output "
    "amplitude must not exceed 3.3 V peak-to-peak. It operates at "
    "room temperature (20-25 C). Standard analog circuit."
)


@pytest_asyncio.fixture
async def live_clients():
    http_clients.init_clients()
    yield
    await http_clients.close_clients()


async def _model_reachable() -> bool:
    """The default role is ``model_general`` which dispatches to the
    Ollama provider against the host's :11434. We probe that directly
    so the skip message is specific."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{settings.ollama_base_url}/api/tags")
            r.raise_for_status()
            return True
    except Exception:
        return False


# §17.335 — Per-test timeout override. Same pattern as §17.325 / §17.334.
# Live LLM extraction round-trip (cloud 235b) on a ~4 KB schema-heavy
# prompt routinely runs 10-30 s; the suite-wide `pytest --timeout=30`
# is too tight for the natural variance, especially under contention.
@pytest.mark.smoke
@pytest.mark.timeout(900)
async def test_extract_spec_live_unambiguous_brief(live_clients):
    if os.environ.get("SCAFFOLD_SKIP_LIVE_LLM") == "1":
        pytest.skip("SCAFFOLD_SKIP_LIVE_LLM=1")
    if not await _model_reachable():
        pytest.skip(f"ollama unreachable at {settings.ollama_base_url}")

    async with async_session() as db:
        result = await extract_spec(UNAMBIGUOUS_BRIEF, db=db)

    # The brief includes every required quantity; the LLM should not
    # come back with ambiguities. If it does, the prompt or the model
    # has drifted — surface enough of the response for diagnosis.
    assert result.ok, (
        f"live extraction failed: errors={result.errors!r} "
        f"ambiguities={result.ambiguities!r} "
        f"raw_tail={result.llm_raw_text[-400:]!r}"
    )
    assert result.spec_id is not None
    assert validate_spec(result.spec).ok, (
        f"spec failed re-validation post-INSERT: {validate_spec(result.spec).errors!r}"
    )

    # Spot-check that the LLM honored the brief's key numbers.
    fc_constraints = [
        c for c in result.spec["constraints"]
        if c["kind"] == "electrical.frequency"
    ]
    assert fc_constraints, "LLM dropped the corner-frequency constraint"
    assert abs(fc_constraints[0].get("target", 0) - 1000.0) < 1.0

    async with async_session() as db:
        row = await db.execute(
            text(
                """
                SELECT schema_version, spec_sha256, confirmed_at
                FROM specs
                WHERE id = :id
                """
            ),
            {"id": str(result.spec_id)},
        )
        persisted = row.mappings().one()

    assert persisted["schema_version"] == "1.0.0"
    assert persisted["confirmed_at"] is None  # /confirm gate not wired yet

    async with async_session() as db:
        await db.execute(
            text("DELETE FROM specs WHERE id = :id"),
            {"id": str(result.spec_id)},
        )
        await db.commit()
