"""Expose the Scaffold Engine itself as an MCP server (§17.772, producer side).

One ``MCPServer`` instance publishes the engine's high-value capabilities as
MCP tools. It is served two ways:

  * **Streamable HTTP**, mounted at ``/mcp`` on the main orchestrator (gated by
    ``settings.mcp_server_enabled``, guarded by X-API-Key). See ``main.py``.
  * **stdio**, via ``python -m app.mcp_server`` — for desktop MCP clients that
    launch a subprocess (e.g. ``docker exec -i scaffold-orchestrator python -m
    app.mcp_server``). Always available (exec-gated, not flag-gated).

Tool handlers call the engine's internal module functions **directly** — never
via a loopback HTTP request. Under the single-worker uvicorn model a mounted
in-process tool that looped back to ``:8000`` would deadlock the event loop
(the §web-loopback lesson); calling the functions directly also skips a
redundant round of auth + serialization.

Long-running work (run_job, research) is spawned as a background task and the
tool returns immediately with an id to poll — a single MCP tool call can't
block for the minutes/hours a full build takes.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from uuid import UUID

from mcp.server import MCPServer
from sqlalchemy import text

from app.config import settings
from app.database import async_session

logger = logging.getLogger("scaffold.mcp")

SERVER_VERSION = "1.0.0"

mcp = MCPServer(
    name="scaffold-engine",
    version=SERVER_VERSION,
    instructions=(
        "Scaffold Engine — a self-hosted DAG orchestration engine for multi-step "
        "LLM workflows. Use `ideate` to turn an idea into a feasibility-checked "
        "job, `run_job` to execute it (research → DAG → run) in the background, "
        "and `job_status`/`job_results` to track and collect output. `rag_query` "
        "searches the ingested knowledge corpus; `research` runs autonomous web "
        "research."
    ),
)

# Strong refs to detached background tasks so they are not GC'd mid-flight.
_BG_TASKS: set[asyncio.Task] = set()


def _spawn(coro) -> asyncio.Task:
    task = asyncio.create_task(coro)
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)
    return task


def _valid_uuid(job_id: str) -> UUID | None:
    try:
        return UUID(job_id)
    except (ValueError, AttributeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
@mcp.tool()
async def ideate(idea: str, domain: str | None = None) -> dict[str, Any]:
    """Turn a raw idea into a refined brief + feasibility assessment, creating a
    job that halts awaiting confirmation. Returns the job_id and the assessment;
    call `run_job(job_id)` to execute it."""
    from app.modules.ideation_workflow import analyze_and_confirm, get_ideation_slot_sem

    async with get_ideation_slot_sem():
        async with async_session() as db:
            result = await analyze_and_confirm(idea, db, domain=domain)
    return result if isinstance(result, dict) else {"result": str(result)}


@mcp.tool()
async def run_job(job_id: str) -> dict[str, Any]:
    """Execute a confirmed job end-to-end in the background: Phase-2 research →
    DAG generation → node execution. Returns immediately; poll `job_status`."""
    if _valid_uuid(job_id) is None:
        return {"error": "job_id must be a valid UUID", "job_id": job_id}

    async def _drive() -> None:
        from app.modules.dag_generator import generate_dag
        from app.modules.execution_agent import execute_all_nodes
        from app.modules.ideation_workflow import research_and_compile

        try:
            async with async_session() as db:
                r = await research_and_compile(job_id, db)
            if isinstance(r, dict) and r.get("error"):
                logger.error("mcp_run_job: research failed job=%s err=%s", job_id, r["error"])
                return
            async with async_session() as db:
                r = await generate_dag(job_id, db)
            if isinstance(r, dict) and r.get("error"):
                logger.error("mcp_run_job: dag failed job=%s err=%s", job_id, r["error"])
                return
            # execute_all_nodes is an SSE async generator; drain it to run the DAG.
            async for _ in execute_all_nodes(job_id):
                pass
            logger.info("mcp_run_job: completed job=%s", job_id)
        except Exception:
            logger.exception("mcp_run_job: background run failed job=%s", job_id)

    _spawn(_drive())
    return {
        "job_id": job_id,
        "status": "running",
        "note": "Job is executing in the background. Poll job_status(job_id).",
    }


@mcp.tool()
async def job_status(job_id: str) -> dict[str, Any]:
    """Execution state for a job: overall status plus per-node status counts."""
    uid = _valid_uuid(job_id)
    if uid is None:
        return {"error": "job_id must be a valid UUID", "job_id": job_id}
    from app.modules.execution_handler import execution_status

    async with async_session() as db:
        return await execution_status(uid, db)


@mcp.tool()
async def job_results(job_id: str) -> dict[str, Any]:
    """The compiled deliverable for a job plus each node's output text."""
    uid = _valid_uuid(job_id)
    if uid is None:
        return {"error": "job_id must be a valid UUID", "job_id": job_id}
    from app.modules.execution_handler import node_outputs

    async with async_session() as db:
        row = (await db.execute(
            text(
                "SELECT title, status, deliverable_kind, compiled_output "
                "FROM jobs WHERE id = :id"
            ),
            {"id": job_id},
        )).mappings().first()
        if not row:
            return {"error": f"job not found: {job_id}"}
        nodes = await node_outputs(uid, db)
    return {
        "job_id": job_id,
        "title": row["title"],
        "status": row["status"],
        "deliverable_kind": row["deliverable_kind"],
        "compiled_output": row["compiled_output"],
        "nodes": nodes.get("nodes") if isinstance(nodes, dict) else nodes,
    }


@mcp.tool()
async def list_jobs(limit: int = 20, status: str | None = None) -> dict[str, Any]:
    """List recent jobs (id, title, status, timestamps), newest first. Optionally
    filter by status (e.g. 'completed', 'executing', 'awaiting_confirmation')."""
    limit = max(1, min(int(limit), 100))
    sql = (
        "SELECT id, title, status, job_type, created_at, completed_at "
        "FROM jobs {where} ORDER BY created_at DESC LIMIT :lim"
    )
    params: dict[str, Any] = {"lim": limit}
    where = ""
    if status:
        where = "WHERE status = :st"
        params["st"] = status
    async with async_session() as db:
        rows = (await db.execute(text(sql.format(where=where)), params)).mappings().all()
    return {
        "count": len(rows),
        "jobs": [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "status": r["status"],
                "job_type": r["job_type"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            }
            for r in rows
        ],
    }


@mcp.tool()
async def rag_query(
    query: str, top_k: int = 8, domain: str | None = None
) -> dict[str, Any]:
    """Search the ingested knowledge corpus (embed → vector+keyword → rerank)
    and return the top matching passages."""
    from app.modules.rag_pipeline import query_rag

    result = await query_rag(query, top_k=max(1, min(int(top_k), 50)), domain=domain)
    return result if isinstance(result, dict) else {"result": result}


@mcp.tool()
async def research(
    topic: str, depth: str = "standard", domain: str | None = None
) -> dict[str, Any]:
    """Start autonomous web research on a topic (decompose → search → extract →
    ingest → iterate) in the background. Returns immediately; use
    `research_sessions` to find the resulting session and its status."""
    from app.modules.research_agent import run_research

    async def _drive() -> None:
        try:
            async for _ in run_research(topic=topic, depth=depth, domain=domain):
                pass
        except Exception:
            logger.exception("mcp_research: background run failed topic=%s", topic)

    _spawn(_drive())
    return {
        "status": "researching",
        "topic": topic,
        "note": "Research is running in the background. Use research_sessions() to track it.",
    }


@mcp.tool()
async def research_sessions(limit: int = 10) -> dict[str, Any]:
    """List recent autonomous-research sessions (topic, status, coverage), newest
    first — the way to locate a session started by `research`."""
    limit = max(1, min(int(limit), 50))
    async with async_session() as db:
        rows = (await db.execute(
            text(
                "SELECT id, topic, status, depth, coverage_pct, "
                "total_entries_ingested, created_at, completed_at "
                "FROM research_sessions ORDER BY created_at DESC LIMIT :lim"
            ),
            {"lim": limit},
        )).mappings().all()
    return {
        "count": len(rows),
        "sessions": [
            {
                "id": str(r["id"]),
                "topic": r["topic"],
                "status": r["status"],
                "depth": r["depth"],
                "coverage_pct": r["coverage_pct"],
                "entries_ingested": r["total_entries_ingested"],
                "created_at": r["created_at"].isoformat() if r["created_at"] else None,
                "completed_at": r["completed_at"].isoformat() if r["completed_at"] else None,
            }
            for r in rows
        ],
    }


# ---------------------------------------------------------------------------
# HTTP mount plumbing (used by main.py)
# ---------------------------------------------------------------------------
def streamable_http_app():
    """The Starlette ASGI app for the Streamable-HTTP transport, to mount at
    ``/mcp``. Its session manager must be run in the parent app's lifespan via
    ``session_manager_context()``.

    ``streamable_http_path='/'`` so that mounting the app under ``/mcp`` serves
    the endpoint at ``/mcp`` (the SDK default ``/mcp`` would double to
    ``/mcp/mcp``). ``transport_security`` allows the reverse-proxy/bridge hosts
    the engine is reached by — the endpoint is already X-API-Key gated, so the
    SDK's DNS-rebinding host allowlist would otherwise reject legitimate
    non-localhost Host headers with a 421."""
    from mcp.server.transport_security import TransportSecuritySettings

    return mcp.streamable_http_app(
        streamable_http_path="/",
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )


def session_manager_context():
    """Async context manager that runs the Streamable-HTTP session manager for
    the life of the parent app. Enter it around the orchestrator's lifespan
    ``yield`` when ``settings.mcp_server_enabled``."""
    return mcp.session_manager.run()


class ApiKeyASGIGuard:
    """Minimal ASGI middleware enforcing X-API-Key on the mounted MCP app.

    A mounted sub-app does NOT inherit the parent FastAPI's route dependencies,
    so the engine's global ``require_api_key`` gate does not cover ``/mcp``.
    This wrapper reinstates it: HTTP requests must carry the matching
    ``X-API-Key`` header (constant-time compared). Non-HTTP scopes pass through.

    §17.810 — ADMIN-ONLY BY DESIGN under multi-user. Compares only against the
    master key, so scoped keys get 401. The MCP SDK does not thread a request
    principal into tool handlers, and the tool SQL (list_jobs / job_results /
    research_sessions) is unscoped; admitting scoped keys here would expose every
    user's jobs. MCP is therefore an admin/operator surface — per-user access is
    via the direct JSON API + /ui SPA, which resolve and enforce a Principal.
    """

    def __init__(self, app, api_key: str):
        self.app = app
        self._key = api_key

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or settings.scaffold_auth_disabled:
            await self.app(scope, receive, send)
            return
        import secrets

        headers = dict(scope.get("headers") or [])
        presented = headers.get(b"x-api-key", b"").decode("latin-1")
        if self._key and secrets.compare_digest(presented, self._key):
            await self.app(scope, receive, send)
            return
        await _send_401(send)


async def _send_401(send) -> None:
    body = b'{"detail":"Invalid or missing X-API-Key"}'
    await send({
        "type": "http.response.start",
        "status": 401,
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
        ],
    })
    await send({"type": "http.response.body", "body": body})


# ---------------------------------------------------------------------------
# stdio entrypoint — `python -m app.mcp_server`
# ---------------------------------------------------------------------------
def main() -> None:
    import anyio

    logging.basicConfig(level=logging.INFO)
    logger.info("scaffold-engine MCP server starting on stdio")
    anyio.run(mcp.run_stdio_async)


if __name__ == "__main__":
    main()
