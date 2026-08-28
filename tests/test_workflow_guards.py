"""§17.820 (plan 5.9) — pure-API guard tests relocated from the /web suites.

Neither of these ever touched /web routes — they lived in test_web_ui.py by
historical accident and were the ONLY coverage of these guards:
  - POST /execute/all rejects a non-UUID job_id with a clean 400 BEFORE
    streaming (§17.470 — a raw asyncpg DataError would otherwise leak into an
    execution_failed SSE event).
  - GET /jobs validates limit/offset bounds (422).
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.main import app


@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        with TestClient(app, follow_redirects=False) as tc:
            yield tc
    finally:
        app.dependency_overrides.pop(require_api_key, None)


class TestExecuteAllJobIdGuard:
    """§17.470 — /execute/all must reject a non-UUID job_id with a clean 400
    *before* streaming. Without the guard the id reaches the first asyncpg query
    inside execute_all_nodes and its raw DataError leaks to the client as an
    execution_failed SSE event (info disclosure). Mirrors exec_status's guard."""

    def test_rejects_non_uuid(self, client):
        resp = client.post("/execute/all", json={"job_id": "not-a-uuid"})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid job_id format"

    def test_well_formed_uuid_passes_guard(self, client, monkeypatch):
        """A syntactically-valid UUID clears the guard and reaches streaming.

        Both downstream dependencies are stubbed so this asserts the guard in
        isolation with NO live services: ``_require_valid_models`` (which would
        otherwise 503 against an unreachable Ollama — CI has none) and the
        executor generator itself."""
        async def _models_ok(overrides=None):
            return None  # no-op: the endpoint ignores the return, only the raise matters
        async def _fake_gen(job_id, model_overrides=None):
            yield 'event: pipeline_complete\ndata: {}\n\n'
        monkeypatch.setattr(
            "app.routers.workflow._require_valid_models", _models_ok,
        )
        monkeypatch.setattr(
            "app.routers.workflow.execute_all_nodes", _fake_gen,
        )
        resp = client.post(
            "/execute/all",
            json={"job_id": "00000000-0000-0000-0000-000000000000"},
        )
        assert resp.status_code == 200
        assert "pipeline_complete" in resp.text


class TestJobsListParamGuards:
    def test_rejects_bad_limit(self, client):
        resp = client.get("/jobs?limit=999")
        assert resp.status_code == 422

    def test_rejects_negative_offset(self, client):
        resp = client.get("/jobs?offset=-1")
        assert resp.status_code == 422


class TestAdvanceEndpoint:
    """§17.855 (audit F6) — POST /jobs/{id}/advance is the server-side auto-chain
    (research+compile → plan → optional execute) so CLI/SDK/curl get the macro
    the OWUI pipeline composed client-side."""

    def test_rejects_non_uuid(self, client):
        resp = client.post("/jobs/not-a-uuid/advance", json={})
        assert resp.status_code == 400
        assert resp.json()["detail"] == "Invalid job_id format"

    def _wire(self, monkeypatch, *, research, dag, execute_gen=None):
        class _CM:
            async def __aenter__(self_): return object()
            async def __aexit__(self_, *a): return False
        async def _models_ok(overrides=None): return None
        async def _overrides(job_id, mo): return mo or {}
        async def _visible(*a, **k): return None
        async def _research(job_id, db, **k): return research
        async def _dag(job_id, db, **k): return dag
        monkeypatch.setattr("app.routers.workflow.async_session", lambda: _CM())
        monkeypatch.setattr("app.routers.workflow.assert_visible", _visible)
        monkeypatch.setattr("app.routers.workflow.resolve_job_overrides", _overrides)
        monkeypatch.setattr("app.routers.workflow._require_valid_models", _models_ok)
        monkeypatch.setattr("app.routers.workflow.research_and_compile", _research)
        monkeypatch.setattr("app.routers.workflow._generate_dag", _dag)
        if execute_gen is not None:
            monkeypatch.setattr("app.routers.workflow.execute_all_nodes", execute_gen)

    _UUID = "00000000-0000-0000-0000-000000000000"

    def test_plan_only_emits_phases_and_complete(self, client, monkeypatch):
        self._wire(monkeypatch, research={"ok": True}, dag={"node_count": 3})
        resp = client.post(f"/jobs/{self._UUID}/advance", json={"execute": False})
        assert resp.status_code == 200
        assert '"phase": "research"' in resp.text
        assert '"phase": "planning"' in resp.text
        assert "advance_complete" in resp.text and '"node_count": 3' in resp.text
        assert "pipeline_complete" not in resp.text  # no execution

    def test_execute_streams_the_run(self, client, monkeypatch):
        async def _exec(job_id, model_overrides=None):
            yield "event: pipeline_complete\ndata: {}\n\n"
        self._wire(monkeypatch, research={"ok": True}, dag={"node_count": 1},
                   execute_gen=_exec)
        resp = client.post(f"/jobs/{self._UUID}/advance", json={"execute": True})
        assert resp.status_code == 200
        assert "pipeline_complete" in resp.text
        assert "advance_complete" not in resp.text  # execution replaces the terminal

    def test_research_error_stops_the_chain(self, client, monkeypatch):
        self._wire(monkeypatch,
                   research={"error": "brief missing", "http_status": 409},
                   dag={"node_count": 3})
        resp = client.post(f"/jobs/{self._UUID}/advance", json={})
        assert resp.status_code == 200  # SSE stream; error is an event
        assert '"phase": "research"' in resp.text
        assert "brief missing" in resp.text
        assert '"phase": "planning"' not in resp.text  # never reached planning
