"""Synchronous client for the Scaffold Engine orchestrator.

The top-level workflow methods (``ideate``, ``confirm``, ``execute``,
``optimize``, ``skip``, ``health``, ``status``) live directly on
``Client``. Larger groupings live on resource sub-objects:
``client.jobs``, ``client.dag``, ``client.prompts``, ``client.gt``,
``client.rag``, ``client.schedule``, ``client.assist``,
``client.research``, ``client.models``, ``client.observability``.

SSE-streamed endpoints (``/research``, ``/execute/all``, ``/research/reply``,
``/research/pdf``) are served by ``AsyncClient`` only — see ``async_client``.
"""
from __future__ import annotations

from typing import Any

import httpx

from . import _transport
from ._resources import (
    AssistResource,
    DagResource,
    GtResource,
    JobsResource,
    ModelsResource,
    ObservabilityResource,
    PromptsResource,
    RagResource,
    ResearchResource,
    ScheduleResource,
    _drop_none,
)
from ._version import __version__


class Client:
    """Sync HTTP client for the Scaffold Engine API.

    Pre-injects ``X-API-Key`` when a key is configured. Network errors and
    non-2xx responses raise specific ``ScaffoldError`` subclasses; the
    base class catches them all.
    """

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
        self._http = httpx.Client(
            base_url=self.base_url,
            headers=headers,
            timeout=timeout,
            follow_redirects=True,
        )

        # Resource sub-objects — instantiated once per Client so callers can
        # rely on identity (``c.jobs is c.jobs``) when stashing references.
        self.jobs = JobsResource(self)
        self.dag = DagResource(self)
        self.prompts = PromptsResource(self)
        self.gt = GtResource(self)
        self.rag = RagResource(self)
        self.schedule = ScheduleResource(self)
        self.assist = AssistResource(self)
        self.research = ResearchResource(self)
        self.models = ModelsResource(self)
        self.observability = ObservabilityResource(self)

    # ------------------------------------------------------------------
    # Generic dispatch — typed methods delegate to this.
    # ------------------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        """Generic dispatch. Raises ``ScaffoldError`` subclass on failure."""
        try:
            resp = self._http.request(method, path, params=params, json=json)
        except Exception as exc:
            raise _transport.translate_request_error(exc, url=self.base_url) from None
        _transport.raise_for_status(resp)
        return _transport.parse_body(resp)

    # ------------------------------------------------------------------
    # Top-level workflow methods.
    # ------------------------------------------------------------------

    def health(self) -> dict[str, Any]:
        """``GET /health`` — concurrent dep probe (Postgres, Milvus, Redis, Ollama).

        No auth required; safe to call without an API key.
        """
        return self.request("GET", "/health")

    def status(self) -> dict[str, Any]:
        """``GET /status`` — counts of jobs in each lifecycle state plus recents."""
        return self.request("GET", "/status")

    def config(self) -> dict[str, Any]:
        """``GET /config`` — every Settings field with current value, default,
        and is_default flag. Sensitive fields (``*_key``, ``*_secret``,
        SecretStr-typed) are redacted server-side to ``(set)`` / ``(unset)``.
        """
        return self.request("GET", "/config")

    def logs(self, job_id: str, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """``GET /logs/{job_id}`` — paginated execution logs for one job."""
        return self.request(
            "GET", f"/logs/{job_id}", params={"limit": limit, "offset": offset}
        )

    def ideate(
        self,
        idea: str,
        *,
        domain: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """``POST /ideate`` — Phase 1: refine + assess feasibility, halt for confirmation."""
        body = _drop_none({"idea": idea, "domain": domain, "model": model})
        return self.request("POST", "/ideate", json=body)

    def confirm(
        self,
        job_id: str,
        *,
        feedback: str | None = None,
        push_to_github: bool = False,
    ) -> dict[str, Any]:
        """``POST /ideate/confirm`` — Phase 2: research → ingest → compile workflow.

        Long-running. Tune ``timeout`` on the ``Client`` constructor before
        calling — the orchestrator can take minutes against a cold corpus.
        """
        body = _drop_none({
            "job_id": job_id,
            "feedback": feedback,
            "push_to_github": push_to_github,
        })
        return self.request("POST", "/ideate/confirm", json=body)

    def optimize(
        self,
        prompt: str,
        *,
        model_optimizer: str | None = None,
        model_verifier: str | None = None,
        skip_verify: bool = False,
    ) -> dict[str, Any]:
        """``POST /optimize`` — strip → optimize → verify pipeline for a prompt."""
        body = _drop_none({
            "prompt": prompt,
            "model_optimizer": model_optimizer,
            "model_verifier": model_verifier,
            "skip_verify": skip_verify,
        })
        return self.request("POST", "/optimize", json=body)

    def execute(
        self,
        job_id: str,
        *,
        skip_optimize: bool = False,
        skip_verify: bool = False,
    ) -> dict[str, Any]:
        """``POST /execute`` — execute the next ready DAG node for a job.

        Single-step. For full topological execution use ``AsyncClient.aiter_execute_all``
        (J.1.d) which streams SSE progress events.
        """
        return self.request(
            "POST",
            "/execute",
            json={
                "job_id": job_id,
                "skip_optimize": skip_optimize,
                "skip_verify": skip_verify,
            },
        )

    def skip(self, job_id: str, node_key: str) -> dict[str, Any]:
        """``POST /skip`` — mark a node as ``skipped`` and unblock downstream work."""
        return self.request(
            "POST", "/skip", json={"job_id": job_id, "node_key": node_key}
        )

    # ------------------------------------------------------------------
    # Lifecycle.
    # ------------------------------------------------------------------

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()
