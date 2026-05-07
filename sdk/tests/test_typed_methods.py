"""Typed-method tests for ``Client`` (sync).

Each test mocks ``_http.request`` and asserts the verb, URL, and payload
the SDK sent to the orchestrator. The shared fixtures keep the per-test
boilerplate to a couple of lines.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest

from scaffold_client import Client


def _resp(status: int = 200, payload: dict | list | None = None) -> MagicMock:
    r = MagicMock(spec=httpx.Response)
    r.status_code = status
    r.text = ""
    r.json = MagicMock(return_value=payload if payload is not None else {})
    return r


@pytest.fixture
def client():
    with Client("http://example.com", api_key="k") as c:
        yield c


def _last_call(mock):
    """Return ``(args, kwargs)`` of the most recent mock invocation."""
    return mock.call_args.args, mock.call_args.kwargs


# --------------------------------------------------------------------------
# Top-level workflow
# --------------------------------------------------------------------------


def test_health_get_health(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"status": "ok"})) as m:
        assert client.health() == {"status": "ok"}
    args, _ = _last_call(m)
    assert args == ("GET", "/health")


def test_status_get_status(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"counts": {}})) as m:
        client.status()
    args, _ = _last_call(m)
    assert args == ("GET", "/status")


def test_logs_uses_pagination_params(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"logs": []})) as m:
        client.logs("abc-123", limit=10, offset=5)
    args, kwargs = _last_call(m)
    assert args == ("GET", "/logs/abc-123")
    assert kwargs["params"] == {"limit": 10, "offset": 5}


def test_ideate_minimal_body(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"job_id": "x"})) as m:
        client.ideate("Build a markdown linter")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/ideate")
    assert kwargs["json"] == {"idea": "Build a markdown linter"}


def test_ideate_includes_optional_fields(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"job_id": "x"})) as m:
        client.ideate("idea", domain="eng", model="qwen3:7b")
    _, kwargs = _last_call(m)
    assert kwargs["json"] == {"idea": "idea", "domain": "eng", "model": "qwen3:7b"}


def test_ideate_drops_none_optional_fields(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.ideate("idea", domain=None)
    _, kwargs = _last_call(m)
    assert "domain" not in kwargs["json"]


def test_confirm_minimal(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.confirm("job-123")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/ideate/confirm")
    assert kwargs["json"]["job_id"] == "job-123"
    assert kwargs["json"]["push_to_github"] is False


def test_confirm_with_feedback_and_github(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.confirm("job-123", feedback="add CI", push_to_github=True)
    _, kwargs = _last_call(m)
    assert kwargs["json"]["feedback"] == "add CI"
    assert kwargs["json"]["push_to_github"] is True


def test_optimize_minimal(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.optimize("write a function that sorts a list")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/optimize")
    assert kwargs["json"]["prompt"] == "write a function that sorts a list"
    assert kwargs["json"]["skip_verify"] is False


def test_execute_default_flags(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.execute("job-1")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/execute")
    assert kwargs["json"] == {
        "job_id": "job-1",
        "skip_optimize": False,
        "skip_verify": False,
    }


def test_skip_node(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.skip("job-1", "T2")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/skip")
    assert kwargs["json"] == {"job_id": "job-1", "node_key": "T2"}


# --------------------------------------------------------------------------
# client.jobs.*
# --------------------------------------------------------------------------


def test_jobs_list_default_params(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"jobs": [], "total": 0})) as m:
        client.jobs.list()
    args, kwargs = _last_call(m)
    assert args == ("GET", "/jobs")
    assert kwargs["params"] == {"limit": 25, "offset": 0}


def test_jobs_list_with_filters(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"jobs": []})) as m:
        client.jobs.list(status="completed", q="markdown", limit=50)
    _, kwargs = _last_call(m)
    assert kwargs["params"] == {
        "status": "completed",
        "q": "markdown",
        "limit": 50,
        "offset": 0,
    }


def test_jobs_status(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"job_status": "running"})) as m:
        client.jobs.status("abc")
    args, _ = _last_call(m)
    assert args == ("GET", "/exec/status/abc")


def test_jobs_delete(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"deleted": True})) as m:
        client.jobs.delete("abc")
    args, _ = _last_call(m)
    assert args == ("DELETE", "/jobs/abc")


def test_jobs_update_renames(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"id": "abc"})) as m:
        client.jobs.update("abc", title="new title")
    args, kwargs = _last_call(m)
    assert args == ("PATCH", "/jobs/abc")
    assert kwargs["json"] == {"title": "new title"}


def test_jobs_cleanup(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"reaped": 0})) as m:
        client.jobs.cleanup()
    args, _ = _last_call(m)
    assert args == ("POST", "/jobs/cleanup")


def test_jobs_retry(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.jobs.retry("abc", "T2")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/exec/retry")
    assert kwargs["json"] == {"job_id": "abc", "node_key": "T2"}


# --------------------------------------------------------------------------
# client.dag.*
# --------------------------------------------------------------------------


def test_dag_get(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"nodes": []})) as m:
        client.dag.get("abc")
    args, _ = _last_call(m)
    assert args == ("GET", "/dag/abc")


def test_dag_create_minimal(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.dag.create("abc")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/dag")
    assert kwargs["json"] == {"job_id": "abc"}


def test_dag_create_with_model_override(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.dag.create("abc", model="qwen3:7b")
    _, kwargs = _last_call(m)
    assert kwargs["json"] == {"job_id": "abc", "model": "qwen3:7b"}


# --------------------------------------------------------------------------
# client.prompts.*
# --------------------------------------------------------------------------


def test_prompts_list_for_job(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.prompts.list("abc")
    args, _ = _last_call(m)
    assert args == ("GET", "/prompts/abc")


def test_prompts_get_node(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.prompts.get("abc", "T2")
    args, _ = _last_call(m)
    assert args == ("GET", "/prompts/abc/T2")


def test_prompts_history(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.prompts.history("abc", "T2")
    args, _ = _last_call(m)
    assert args == ("GET", "/prompts/abc/T2/history")


def test_prompts_update_node(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.prompts.update("abc", "T2", "new prompt text")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/prompts/abc/T2")
    assert kwargs["json"] == {"prompt": "new prompt text"}


# --------------------------------------------------------------------------
# client.gt.*
# --------------------------------------------------------------------------


def test_gt_create(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.gt.create("kubernetes", queries=["pods", "services"])
    args, kwargs = _last_call(m)
    assert args == ("POST", "/gt")
    assert kwargs["json"]["topic"] == "kubernetes"
    assert kwargs["json"]["queries"] == ["pods", "services"]


def test_gt_list(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.gt.list(domain="eng", per_page=50)
    args, kwargs = _last_call(m)
    assert args == ("GET", "/gt/list")
    assert kwargs["params"]["domain"] == "eng"
    assert kwargs["params"]["per_page"] == 50


def test_gt_search(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.gt.search("partition keys", top_k=5)
    args, kwargs = _last_call(m)
    assert args == ("POST", "/gt/search")
    assert kwargs["json"]["query"] == "partition keys"
    assert kwargs["json"]["top_k"] == 5


def test_gt_detail(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.gt.detail("entry-123")
    args, _ = _last_call(m)
    assert args == ("GET", "/gt/detail/entry-123")


def test_gt_stats(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.gt.stats()
    args, _ = _last_call(m)
    assert args == ("GET", "/gt/stats")


# --------------------------------------------------------------------------
# client.rag.*
# --------------------------------------------------------------------------


def test_rag_search_minimal(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.rag.search("how do partition keys work")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/rag")
    assert kwargs["json"]["query"] == "how do partition keys work"


def test_rag_search_overrides(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.rag.search("q", top_k=20, skip_rerank=True, domain="rag")
    _, kwargs = _last_call(m)
    assert kwargs["json"]["top_k"] == 20
    assert kwargs["json"]["skip_rerank"] is True
    assert kwargs["json"]["domain"] == "rag"


def test_rag_dedup(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.rag.dedup(limit=20)
    args, kwargs = _last_call(m)
    assert args == ("GET", "/rag/dedup")
    assert kwargs["params"]["limit"] == 20


# --------------------------------------------------------------------------
# client.schedule.*
# --------------------------------------------------------------------------


def test_schedule_list(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"schedules": []})) as m:
        client.schedule.list()
    args, _ = _last_call(m)
    assert args == ("GET", "/schedule")


def test_schedule_create(client):
    with patch.object(client._http, "request", return_value=_resp(200, {})) as m:
        client.schedule.create("kubernetes news", "0 9 * * 1", timezone="America/New_York")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/schedule")
    assert kwargs["json"] == {
        "topic": "kubernetes news",
        "cron_expression": "0 9 * * 1",
        "depth": "medium",
        "timezone": "America/New_York",
    }


def test_schedule_delete(client):
    with patch.object(client._http, "request", return_value=_resp(200, {"deleted": True})) as m:
        client.schedule.delete(42)
    args, _ = _last_call(m)
    assert args == ("DELETE", "/schedule/42")


# --------------------------------------------------------------------------
# Resource sub-objects
# --------------------------------------------------------------------------


def test_resource_subobjects_have_stable_identity(client):
    """``client.jobs is client.jobs`` so callers can stash references."""
    assert client.jobs is client.jobs
    assert client.dag is client.dag
    assert client.prompts is client.prompts
    assert client.gt is client.gt
    assert client.rag is client.rag
    assert client.schedule is client.schedule
