"""Async mirror of test_typed_methods.py.

We patch ``AsyncClient.request`` (an async method) with an ``AsyncMock``
and assert each typed method dispatches with the right verb/path/payload.
This exercises both the resource sub-objects and the top-level workflow
methods on AsyncClient.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from scaffold_client import AsyncClient


@pytest.fixture
async def aclient():
    async with AsyncClient("http://example.com", api_key="k") as c:
        yield c


def _last_call(mock):
    return mock.call_args.args, mock.call_args.kwargs


# --------------------------------------------------------------------------
# Top-level workflow
# --------------------------------------------------------------------------


async def test_async_health(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={"ok": True})) as m:
        assert await aclient.health() == {"ok": True}
    args, _ = _last_call(m)
    assert args == ("GET", "/health")


async def test_async_status(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.status()
    args, _ = _last_call(m)
    assert args == ("GET", "/status")


async def test_async_logs(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.logs("abc", limit=10)
    args, kwargs = _last_call(m)
    assert args == ("GET", "/logs/abc")
    assert kwargs["params"]["limit"] == 10


async def test_async_ideate_drops_nones(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.ideate("idea", domain=None)
    _, kwargs = _last_call(m)
    assert kwargs["json"] == {"idea": "idea"}


async def test_async_confirm(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.confirm("job-1", feedback="x")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/ideate/confirm")
    assert kwargs["json"]["feedback"] == "x"


async def test_async_optimize(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.optimize("p", skip_verify=True)
    _, kwargs = _last_call(m)
    assert kwargs["json"]["skip_verify"] is True


async def test_async_execute(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.execute("j")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/execute")
    assert kwargs["json"] == {"job_id": "j", "skip_optimize": False, "skip_verify": False}


async def test_async_skip(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.skip("j", "T2")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/skip")
    assert kwargs["json"] == {"job_id": "j", "node_key": "T2"}


# --------------------------------------------------------------------------
# Resource sub-objects (one representative test each — full grid in sync suite)
# --------------------------------------------------------------------------


async def test_async_jobs_list(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={"jobs": []})) as m:
        await aclient.jobs.list(status="completed")
    args, kwargs = _last_call(m)
    assert args == ("GET", "/jobs")
    assert kwargs["params"]["status"] == "completed"


async def test_async_jobs_status(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.jobs.status("abc")
    args, _ = _last_call(m)
    assert args == ("GET", "/exec/status/abc")


async def test_async_jobs_traces(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={"traces": []})) as m:
        await aclient.jobs.traces("abc", kind="chat", limit=5)
    args, kwargs = _last_call(m)
    assert args == ("GET", "/trace/abc")
    assert kwargs["params"] == {"limit": 5, "offset": 0, "kind": "chat"}


async def test_async_jobs_update(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.jobs.update("abc", title="renamed")
    args, kwargs = _last_call(m)
    assert args == ("PATCH", "/jobs/abc")
    assert kwargs["json"] == {"title": "renamed"}


async def test_async_dag_get(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.dag.get("abc")
    args, _ = _last_call(m)
    assert args == ("GET", "/dag/abc")


async def test_async_prompts_update(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.prompts.update("abc", "T2", "new prompt")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/prompts/abc/T2")
    assert kwargs["json"] == {"prompt": "new prompt"}


async def test_async_gt_search(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.gt.search("q", top_k=5)
    args, kwargs = _last_call(m)
    assert args == ("POST", "/gt/search")
    assert kwargs["json"]["top_k"] == 5


async def test_async_rag_search(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.rag.search("q")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/rag")
    assert kwargs["json"]["query"] == "q"


async def test_async_schedule_create(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.schedule.create("topic", "0 9 * * 1")
    args, kwargs = _last_call(m)
    assert args == ("POST", "/schedule")
    assert kwargs["json"]["timezone"] == "UTC"


async def test_async_resource_subobjects_have_stable_identity(aclient):
    assert aclient.jobs is aclient.jobs
    assert aclient.dag is aclient.dag
    assert aclient.prompts is aclient.prompts
    assert aclient.gt is aclient.gt
    assert aclient.rag is aclient.rag
    assert aclient.schedule is aclient.schedule
    assert aclient.observability is aclient.observability


# --------------------------------------------------------------------------
# §17.88 — async observability resource (errors triage)
# --------------------------------------------------------------------------


async def test_async_observability_recent_errors_with_filters(aclient):
    with patch.object(aclient, "request",
                      AsyncMock(return_value={"errors": [], "count": 0})) as m:
        await aclient.observability.recent_errors(resolved=False, since_minutes=60)
    args, kwargs = _last_call(m)
    assert args == ("GET", "/observability/errors")
    assert kwargs["params"] == {
        "resolved": False, "since_minutes": 60, "limit": 50,
    }


async def test_async_observability_resolve_error_with_note(aclient):
    with patch.object(aclient, "request", AsyncMock(return_value={})) as m:
        await aclient.observability.resolve_error("abc", resolution="triaged")
    args, kwargs = _last_call(m)
    assert args == ("PATCH", "/observability/errors/abc")
    assert kwargs["json"] == {"resolved": True, "resolution": "triaged"}
