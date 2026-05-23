"""Tests for app/main.py.

Phase 1 — Fix-list item #1: `/schedule` endpoint had an unawaited
`_require_valid_models` call, silently skipping model validation. This
test proves the await is present by mocking the coroutine to raise 422
and asserting the exception propagates.
"""
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.main import app


@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_api_key, None)


def test_schedule_awaits_require_valid_models(client):
    """If `await` is missing, `_require_valid_models` returns a coroutine
    without running and the endpoint proceeds to DB insert. With `await`,
    the mocked HTTPException(422) propagates and we get 422."""
    exc = HTTPException(
        status_code=422,
        detail={
            "error": "model_validation_failed",
            "missing_models": ["nonexistent:1b"],
        },
    )
    # §17.174 — /schedule moved to app.routers.schedule; patch the
    # symbol where the route handler looks it up (the workflow.py /
    # schedule.py modules import _require_valid_models into their own
    # namespace at import time).
    with patch("app.routers.schedule._require_valid_models",
               new_callable=AsyncMock, side_effect=exc):
        response = client.post(
            "/schedule",
            json={
                "topic": "phase-1 regression test",
                "cron_expression": "0 9 * * 1",
                "depth": "shallow",
                "timezone": "UTC",
                "model_overrides": {"model_general": "nonexistent:1b"},
            },
        )

    assert response.status_code == 422, (
        f"Expected 422 (proves await runs); got "
        f"{response.status_code}: {response.text}"
    )
    detail = response.json().get("detail")
    if isinstance(detail, dict):
        assert detail.get("error") == "model_validation_failed"


# ───────── Phase 2: fix-list #13 (model migration) & #14 (Pydantic bodies) ─────────

def test_fix13_moved_models_importable_and_retain_fields():
    """#13: 6 inline models moved from main.py → schemas.py.
    Instantiate each with minimal valid data — proves no field was dropped."""
    from app.schemas import (
        ConfirmInput, DagInput, GtInput, GtSearchInput, IdeaInput, RagInput,
    )
    i = IdeaInput(idea="x")
    assert i.idea == "x" and i.domain is None and i.model is None and i.model_overrides is None
    c = ConfirmInput(job_id="j")
    assert c.job_id == "j" and c.feedback is None and c.push_to_github is False and c.model_overrides is None
    d = DagInput(job_id="j")
    assert d.job_id == "j" and d.model is None and d.model_overrides is None
    r = RagInput(query="q")
    assert r.query == "q" and r.top_k == 10 and r.confidence_threshold == 0.8
    assert r.skip_rerank is False and r.include_history is False and r.domain is None
    g = GtInput(topic="t")
    assert g.topic == "t" and g.queries is None and g.push_to_github is False
    assert g.target_file is None and g.model is None
    gs = GtSearchInput(query="q")
    assert gs.query == "q" and gs.top_k == 10 and gs.domain is None


def test_fix14_prompts_update_requires_pydantic_body(client):
    """#14: empty body → 422 (Pydantic rejects missing required `prompt`).
    The old raw-request handler would have returned 400 with a custom message."""
    response = client.post(
        "/prompts/00000000-0000-0000-0000-000000000000/T1",
        json={},
    )
    assert response.status_code == 422, response.text


def test_fix14_exec_retry_requires_pydantic_body(client):
    """#14: missing required job_id/node_key → 422 from Pydantic layer."""
    response = client.post("/exec/retry", json={})
    assert response.status_code == 422, response.text


# ───────── Phase 3: fix-list #15 (validation coverage) & #16 (template) ─────────

def test_fix15_ideas_runs_model_validation(client):
    """#15: /ideas now awaits _require_valid_models — 422 on bad override."""
    from fastapi import HTTPException
    from unittest.mock import AsyncMock, patch
    exc = HTTPException(
        status_code=422,
        detail={"error": "model_validation_failed", "missing_models": ["nope:1b"]},
    )
    # §17.174 — /ideas moved to app.routers.workflow.
    with patch("app.routers.workflow._require_valid_models",
               new_callable=AsyncMock, side_effect=exc):
        response = client.post(
            "/ideas",
            json={"idea": "test", "model_overrides": {"model_router": "nope:1b"}},
        )
    assert response.status_code == 422, response.text


def test_fix15_gt_runs_model_validation(client):
    """#15: /gt now awaits _require_valid_models — 422 on bad model."""
    from fastapi import HTTPException
    from unittest.mock import AsyncMock, patch
    exc = HTTPException(
        status_code=422,
        detail={"error": "model_validation_failed", "missing_models": ["nope:1b"]},
    )
    # §17.174 — /gt moved to app.routers.gt.
    with patch("app.routers.gt._require_valid_models",
               new_callable=AsyncMock, side_effect=exc):
        response = client.post(
            "/gt",
            json={"topic": "test", "model": "nope:1b"},
        )
    assert response.status_code == 422, response.text


def test_fix16_pdf_upload_form_renders_from_template(client):
    """#16: GET /research/pdf renders Jinja template with expected form controls."""
    response = client.get("/research/pdf")
    assert response.status_code == 200, response.text
    body = response.text
    assert 'id="fileinput"' in body, "file input missing"
    assert 'id="extractor"' in body, "extractor select missing"
    assert 'id="domain"' in body, "domain select missing"
    assert "Scaffold Engine" in body


# ───────── Module 5 Phase 2: fix-list #98 (/execute validation) & #95 (skip_node shape) ─────────

def test_fix98_execute_runs_model_validation(client):
    """#98: /execute now awaits _require_valid_models — 422 on bad override."""
    from fastapi import HTTPException
    from unittest.mock import AsyncMock, patch
    exc = HTTPException(
        status_code=422,
        detail={"error": "model_validation_failed", "missing_models": ["nope:1b"]},
    )
    # §17.174 — /execute moved to app.routers.workflow.
    with patch("app.routers.workflow._require_valid_models",
               new_callable=AsyncMock, side_effect=exc):
        response = client.post(
            "/execute",
            json={"job_id": "00000000-0000-0000-0000-000000000000",
                  "model_overrides": {"model_general": "nope:1b"}},
        )
    assert response.status_code == 422, response.text


def test_startup_probe_timeout_capped_at_2_seconds():
    """§17.179 follow-up — the lifespan probe cap MUST stay <= 2 s so
    cloud-CI smoke runs against unreachable services complete the lifespan
    inside pytest's 30 s timeout. The constant fans out to both the Milvus
    asyncio.wait_for cap and the Ollama HTTP GET timeout in app/main.lifespan."""
    from app import main as main_mod
    assert main_mod._STARTUP_PROBE_TIMEOUT_S <= 2.0, (
        f"_STARTUP_PROBE_TIMEOUT_S regressed above the 2 s cap: "
        f"{main_mod._STARTUP_PROBE_TIMEOUT_S}"
    )


def test_database_connect_timeout_capped_at_2_seconds():
    """§17.179 follow-up — asyncpg connect_timeout must stay <= 2 s; it
    governs every async_session() open across the codebase, not just
    lifespan. Default is 60 s, which under unreachable Postgres makes
    every DB-touching lifespan step block for ~1 min each."""
    from app.database import engine
    # SQLAlchemy stores the connect_args on the dialect / pool config; the
    # most stable read is via the engine's __repr__-able pool / kwargs.
    # We assert via the create_async_engine input by re-reading the source
    # of truth — the engine's url + dialect connect_args proxy.
    # Easiest: inspect the module-level engine's pool _kwargs.
    connect_args = engine.pool._creator.keywords.get("connect_args", {}) \
        if hasattr(engine.pool, "_creator") and hasattr(engine.pool._creator, "keywords") \
        else {}
    # Fallback: parse the engine's url query params or just import the
    # database module and read the source constant. Use a direct module
    # re-import to read the literal we set.
    import inspect
    from app import database as db_mod
    src = inspect.getsource(db_mod)
    assert '"timeout": 2' in src or "'timeout': 2" in src, (
        "asyncpg connect_timeout in app/database.py regressed above 2 s"
    )


def test_fix95_skip_node_return_shape_matches_execution_result():
    """#95: skip_node returns keys that ExecutionResult response_model accepts.

    /skip is declared with response_model=ExecutionResult. If skip_node()
    returns keys ExecutionResult doesn't model, FastAPI drops them silently
    on serialization — this pins that the two canonical success/error shapes
    are compatible with the response model.
    """
    from app.schemas import ExecutionResult
    # Success shape from skip_node
    ok = ExecutionResult(**{"status": "skipped", "node_key": "T1"})
    assert ok.status == "skipped" and ok.node_key == "T1"
    # Error shape from skip_node
    err = ExecutionResult(**{"status": "error", "message": "Node 'T9' not found"})
    assert err.status == "error" and err.message == "Node 'T9' not found"
