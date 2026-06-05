"""Asynchronous client for the Scaffold Engine orchestrator.

Mirrors ``Client`` (sync) for non-streaming endpoints — same resource
sub-objects, same method signatures, just ``await``-able. Adds streaming
helpers for the four SSE-based endpoints:

- ``aiter_research(topic, ...)``        → ``POST /research``
- ``aiter_research_reply(session_id, reply)`` → ``POST /research/reply``
- ``aiter_research_pdf(pdf, ...)``       → ``POST /research/pdf`` (multipart)
- ``aiter_execute_all(job_id, ...)``     → ``POST /execute/all``
- ``aiter_resume_job(job_id, ...)``      → ``POST /jobs/{job_id}/resume``

Each yields ``{"event": str, "data": Any}`` dicts. Heartbeat comments
(``: keepalive``) are filtered by default; pass ``include_heartbeats=True``
to surface them. Breaking out of the ``async for`` cleanly disconnects
from the orchestrator (the keepalive watchdog finalizes the session as
``cancelled``).
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any, AsyncIterator, Literal

import httpx

from . import _transport
from ._async_resources import (
    AsyncAssistResource,
    AsyncDagResource,
    AsyncGtResource,
    AsyncJobsResource,
    AsyncModelsResource,
    AsyncObservabilityResource,
    AsyncPromptsResource,
    AsyncRagResource,
    AsyncResearchResource,
    AsyncScheduleResource,
)
from ._resources import _drop_none
from ._sse import parse_sse_lines
from ._version import __version__


class AsyncClient:
    """Async HTTP client for the Scaffold Engine API."""

    def __init__(
        self,
        base_url: str = "http://localhost:8000",
        api_key: str | None = None,
        *,
        timeout: float = 30.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        headers: dict[str, str] = {"User-Agent": f"scaffold-client/{__version__}"}
        if api_key:
            headers["X-API-Key"] = api_key
        self._http = httpx.AsyncClient(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            # §17.421 — a JSON API client has no reason to follow redirects, and
            # httpx does NOT strip custom headers (X-API-Key) on a cross-host
            # 3xx, so following one would leak the key to the redirect target.
            follow_redirects=False,
        )

        self.jobs = AsyncJobsResource(self)
        self.dag = AsyncDagResource(self)
        self.prompts = AsyncPromptsResource(self)
        self.gt = AsyncGtResource(self)
        self.rag = AsyncRagResource(self)
        self.schedule = AsyncScheduleResource(self)
        self.assist = AsyncAssistResource(self)
        self.research = AsyncResearchResource(self)
        self.models = AsyncModelsResource(self)
        self.observability = AsyncObservabilityResource(self)

    # ------------------------------------------------------------------
    # Generic dispatch
    # ------------------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Generic dispatch. Raises ``ScaffoldError`` subclass on failure."""
        try:
            resp = await self._http.request(method, path, params=params, json=json)
        except Exception as exc:
            raise _transport.translate_request_error(exc, url=self.base_url) from None
        _transport.raise_for_status(resp)
        return _transport.parse_body(resp)

    # ------------------------------------------------------------------
    # Top-level workflow methods (mirror Client)
    # ------------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        return await self.request("GET", "/health")

    async def status(self) -> dict[str, Any]:
        return await self.request("GET", "/status")

    async def config(self) -> dict[str, Any]:
        """``GET /config`` — see ``Client.config`` for redaction details."""
        return await self.request("GET", "/config")

    async def logs(self, job_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return await self.request(
            "GET", f"/logs/{job_id}", params={"limit": limit, "offset": offset}
        )

    async def ideate(
        self,
        idea: str,
        *,
        domain: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        body = _drop_none({"idea": idea, "domain": domain, "model": model})
        return await self.request("POST", "/ideate", json=body)

    async def confirm(
        self,
        job_id: str,
        *,
        feedback: str | None = None,
        push_to_github: bool = False,
    ) -> dict[str, Any]:
        body = _drop_none({
            "job_id": job_id,
            "feedback": feedback,
            "push_to_github": push_to_github,
        })
        return await self.request("POST", "/ideate/confirm", json=body)

    async def optimize(
        self,
        prompt: str,
        *,
        model_optimizer: str | None = None,
        model_verifier: str | None = None,
        skip_verify: bool = False,
    ) -> dict[str, Any]:
        body = _drop_none({
            "prompt": prompt,
            "model_optimizer": model_optimizer,
            "model_verifier": model_verifier,
            "skip_verify": skip_verify,
        })
        return await self.request("POST", "/optimize", json=body)

    async def execute(
        self,
        job_id: str,
        *,
        skip_optimize: bool = False,
        skip_verify: bool = False,
    ) -> dict[str, Any]:
        return await self.request(
            "POST",
            "/execute",
            json={
                "job_id": job_id,
                "skip_optimize": skip_optimize,
                "skip_verify": skip_verify,
            },
        )

    async def skip(self, job_id: str, node_key: str) -> dict[str, Any]:
        return await self.request(
            "POST", "/skip", json={"job_id": job_id, "node_key": node_key}
        )

    # ------------------------------------------------------------------
    # SSE streaming
    # ------------------------------------------------------------------

    async def _stream(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, Any] | None = None,
        include_heartbeats: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Open an SSE stream and yield parsed event dicts.

        Errors at request time (connect/timeout) raise the usual
        ``ScaffoldError`` subclasses. Non-2xx responses are converted by
        ``_transport.raise_for_status`` after the body is read. Mid-stream
        connection errors propagate as raw httpx exceptions — the caller
        should treat ``async for`` interruption as an abnormal end.

        §17.421 — httpx's ``.stream()`` is LAZY: it returns a context manager
        and the connect happens on ``__aenter__``, NOT on the ``.stream()``
        call. So the connect/timeout translation MUST wrap the enter; the
        pre-§17.421 code wrapped the bare (never-raising) ``.stream()`` call,
        which leaked raw ``httpx.ConnectError`` / ``TimeoutException`` out of
        every streaming endpoint when the orchestrator was down.
        """
        stream_ctx = self._http.stream(
            method, path, params=params, json=json, files=files, data=data,
        )
        try:
            resp = await stream_ctx.__aenter__()
        except Exception as exc:
            raise _transport.translate_request_error(exc, url=self.base_url) from None

        try:
            if resp.status_code >= 400:
                # Drain the body so error mapping has a meaningful detail.
                await resp.aread()
                _transport.raise_for_status(resp)

            async for event in parse_sse_lines(
                resp.aiter_lines(), include_heartbeats=include_heartbeats
            ):
                yield event
        finally:
            # Always close the stream — covers normal completion, a
            # raise_for_status ScaffoldError, a mid-stream httpx error (which
            # still propagates raw, as documented), and the clean-disconnect
            # path when the consumer breaks out of the ``async for``.
            await stream_ctx.__aexit__(None, None, None)

    async def aiter_research(
        self,
        topic: str,
        *,
        depth: Literal["shallow", "medium", "deep"] = "medium",
        domain: str | None = None,
        include_heartbeats: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream ``POST /research`` — autonomous topic research.

        Yields events like ``iteration_started``, ``search_complete``,
        ``extraction_complete``, ``contradictions_detected``,
        ``ingestion_complete``, ``convergence``. Terminal events depend
        on the run; downstream code should branch on ``event["event"]``.
        """
        body = _drop_none({"topic": topic, "depth": depth, "domain": domain})
        async for event in self._stream(
            "POST", "/research", json=body, include_heartbeats=include_heartbeats,
        ):
            yield event

    async def aiter_research_reply(
        self,
        session_id: str,
        reply: str,
        *,
        include_heartbeats: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream ``POST /research/reply`` — resume a paused research session."""
        body = {"session_id": session_id, "reply": reply}
        async for event in self._stream(
            "POST", "/research/reply", json=body, include_heartbeats=include_heartbeats,
        ):
            yield event

    async def aiter_research_pdf(
        self,
        pdf: bytes | str | os.PathLike,
        *,
        extractor: Literal["auto", "pypdf", "plumber"] = "auto",
        domain: str | None = None,
        filename: str | None = None,
        include_heartbeats: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream ``POST /research/pdf`` — ingest a PDF (multipart upload)."""
        if isinstance(pdf, (str, os.PathLike)):
            path = Path(pdf)
            pdf_bytes = path.read_bytes()
            inferred_name = path.name
        else:
            pdf_bytes = pdf
            inferred_name = filename or "upload.pdf"
        files = {"file": (filename or inferred_name, pdf_bytes, "application/pdf")}
        params = _drop_none({"extractor": extractor, "domain": domain})
        async for event in self._stream(
            "POST", "/research/pdf",
            params=params, files=files,
            include_heartbeats=include_heartbeats,
        ):
            yield event

    async def aiter_execute_all(
        self,
        job_id: str,
        *,
        skip_optimize: bool = False,
        skip_verify: bool = False,
        include_heartbeats: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream ``POST /execute/all`` — full topological DAG execution."""
        body = {
            "job_id": job_id,
            "skip_optimize": skip_optimize,
            "skip_verify": skip_verify,
        }
        async for event in self._stream(
            "POST", "/execute/all", json=body, include_heartbeats=include_heartbeats,
        ):
            yield event

    async def aiter_resume_job(
        self,
        job_id: str,
        *,
        skip_optimize: bool = False,
        skip_verify: bool = False,
        include_heartbeats: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream ``POST /jobs/{job_id}/resume`` — resume a cancelled job.

        Atomically transitions the job from ``cancelled`` back to
        ``executing`` server-side, then streams the same SSE event shape
        as ``aiter_execute_all``. Raises ``ConflictError`` if the job is
        not currently cancelled, ``NotFoundError`` if the ID is unknown.
        """
        body = {
            "skip_optimize": skip_optimize,
            "skip_verify": skip_verify,
        }
        async for event in self._stream(
            "POST", f"/jobs/{job_id}/resume",
            json=body, include_heartbeats=include_heartbeats,
        ):
            yield event

    async def aiter_assist_handoff(
        self,
        session_id: str,
        node_key: str,
        *,
        mode: Literal["single", "all_remaining"] = "single",
        include_heartbeats: bool = False,
    ) -> AsyncIterator[dict[str, Any]]:
        """Stream ``POST /assist/{session_id}/handoff`` — autonomous step takeover.

        ``mode='single'`` runs one node and returns control to the operator;
        ``mode='all_remaining'`` runs the rest of the DAG. Yields the same
        node-level SSE events as ``aiter_execute_all``.
        """
        body = {"node_key": node_key, "mode": mode}
        async for event in self._stream(
            "POST", f"/assist/{session_id}/handoff",
            json=body, include_heartbeats=include_heartbeats,
        ):
            yield event

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        await self._http.aclose()

    async def __aenter__(self) -> "AsyncClient":
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        await self.aclose()
