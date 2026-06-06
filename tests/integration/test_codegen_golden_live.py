"""§17.428/§17.429 — live codegen golden tier.

The deferred live counterpart to the offline golden harness (§17.428).
Drives each self-contained golden brief as a one-node CodeGen job through
the REAL model and runs the deterministic structural checkers
(tests/_codegen_golden_checks) on the model's actual output_text.

skip_verify=True on purpose: this tier measures RAW codegen output quality
(does the model produce code satisfying the structural assertions for this
brief?), independent of the verifier — the stricter §17.429 verifier has its
own unit tests in test_execution_codegen_verify.py. skip_verify also bypasses
the §17.428 syntax gate so we assert on the unfiltered model output.

Placement: lives in tests/integration/ so the "no live services" CI job
(`pytest -k "not integration"`, test.yml) and tier-1 ci-smoke (collect_ignore
in tests/conftest.py) both exclude it. The dev-image `make test` runs it but
skips when the configured model is unreachable (or SCAFFOLD_SKIP_LIVE_LLM=1).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text

from app.config import settings
from app.database import async_session
from app.modules.execution_agent import execute_next_node
from app.utils import http_clients
from tests._codegen_golden_checks import check_golden

pytestmark = pytest.mark.asyncio

_FIXTURE = Path(__file__).parent.parent / "fixtures" / "codegen_goldens.json"
_GOLDENS = [
    g for g in json.loads(_FIXTURE.read_text())["goldens"]
    if g.get("live_single_node", True)
]


async def _model_reachable() -> bool:
    """Probe the host Ollama (which fronts the cloud model) so the skip
    message is specific — same pattern as test_spec_extractor_live."""
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{settings.ollama_base_url}/api/tags")
            r.raise_for_status()
            return True
    except Exception:
        return False


@pytest_asyncio.fixture
async def live_clients():
    http_clients.init_clients()
    yield
    await http_clients.close_clients()


@pytest_asyncio.fixture
async def tracked_jobs():
    jobs: list[str] = []
    yield jobs
    async with async_session() as s:
        for jid in jobs:
            await s.execute(text("DELETE FROM jobs WHERE id = :j"), {"j": jid})
        await s.commit()


async def _seed_codegen_job(brief_text: str, tracked_jobs: list[str]) -> str:
    """One CodeGen node, pending, marked is_output_node — the whole DAG."""
    async with async_session() as s:
        row = await s.execute(
            text(
                "INSERT INTO jobs (title, input_text, status, refined_brief) "
                "VALUES (:t, :i, 'executing', CAST(:b AS JSONB)) RETURNING id"
            ),
            {
                "t": "codegen-golden",
                "i": brief_text,
                "b": json.dumps({"description": brief_text, "goals": [brief_text]}),
            },
        )
        jid = str(row.scalar_one())
        await s.execute(
            text(
                "INSERT INTO dag_nodes "
                "(job_id, node_key, title, node_type, status, depends_on, "
                " execution_order, tool, prompt_template, is_output_node) "
                "VALUES (:j, 'T1', :title, 'task', 'pending', '{}', 0, "
                "        'CodeGen', :tmpl, TRUE)"
            ),
            {"j": jid, "title": brief_text[:80], "tmpl": brief_text},
        )
        await s.commit()
    tracked_jobs.append(jid)
    return jid


@pytest.mark.timeout(900)
@pytest.mark.parametrize("golden", _GOLDENS, ids=[g["id"] for g in _GOLDENS])
async def test_codegen_golden_live(golden, live_clients, tracked_jobs):
    if os.environ.get("SCAFFOLD_SKIP_LIVE_LLM") == "1":
        pytest.skip("SCAFFOLD_SKIP_LIVE_LLM=1")
    if not await _model_reachable():
        pytest.skip(f"ollama unreachable at {settings.ollama_base_url}")

    jid = await _seed_codegen_job(golden["brief"], tracked_jobs)
    result = await execute_next_node(jid, skip_verify=True)
    assert result.get("status") != "failed", f"execution failed: {result}"

    async with async_session() as s:
        row = (await s.execute(
            text(
                "SELECT output_text FROM dag_nodes "
                "WHERE job_id = :j AND node_key = 'T1'"
            ),
            {"j": jid},
        )).fetchone()
    output = (row[0] if row else "") or ""

    failures = check_golden(golden, output)
    assert failures == [], (
        f"{golden['id']}: structural checks failed: {failures}\n"
        f"--- model output (first 1500 chars) ---\n{output[:1500]}"
    )
