"""Sync resource sub-objects exposed on ``Client``.

Each resource groups endpoints under a common URL prefix. All methods
return parsed JSON and raise ``ScaffoldError`` subclasses on failure
(via ``Client.request``); request bodies and query params are built
inline from kwargs.

Async mirrors land in J.1.d under ``_async_resources.py`` with the
identical signatures.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .client import Client


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    """Drop ``None`` values so the orchestrator sees only intended fields.

    Useful for both query params (avoid ``?status=None``) and JSON bodies
    (avoid serializing ``"depth": null`` when the user wants the default).
    """
    return {k: v for k, v in d.items() if v is not None}


class JobsResource:
    """``client.jobs.*`` — job lifecycle + execution control."""

    def __init__(self, client: "Client"):
        self._client = client

    def list(
        self,
        *,
        status: str | None = None,
        q: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """``GET /jobs`` — paginated job list with optional status / title filter."""
        params = _drop_none({"status": status, "q": q, "limit": limit, "offset": offset})
        return self._client.request("GET", "/jobs", params=params)

    def status(self, job_id: str) -> dict[str, Any]:
        """``GET /exec/status/{job_id}`` — full execution state for a single job."""
        return self._client.request("GET", f"/exec/status/{job_id}")

    def delete(self, job_id: str) -> dict[str, Any]:
        """``DELETE /jobs/{job_id}`` — hard delete; cascades to dag_nodes / logs."""
        return self._client.request("DELETE", f"/jobs/{job_id}")

    def update(self, job_id: str, *, title: str) -> dict[str, Any]:
        """``PATCH /jobs/{job_id}`` — rename a job (title only for now)."""
        return self._client.request("PATCH", f"/jobs/{job_id}", json={"title": title})

    def cleanup(self) -> dict[str, Any]:
        """``POST /jobs/cleanup`` — sweep stale jobs (admin operation)."""
        return self._client.request("POST", "/jobs/cleanup")

    def retry(self, job_id: str, node_key: str) -> dict[str, Any]:
        """``POST /exec/retry`` — reset a failed node back to ``pending``."""
        return self._client.request(
            "POST", "/exec/retry", json={"job_id": job_id, "node_key": node_key}
        )


class DagResource:
    """``client.dag.*`` — DAG inspection + (re)generation."""

    def __init__(self, client: "Client"):
        self._client = client

    def get(self, job_id: str) -> dict[str, Any]:
        """``GET /dag/{job_id}`` — DAG nodes + job status for a job."""
        return self._client.request("GET", f"/dag/{job_id}")

    def create(self, job_id: str, *, model: str | None = None) -> dict[str, Any]:
        """``POST /dag`` — generate a fresh DAG from the refined idea brief."""
        return self._client.request(
            "POST", "/dag", json=_drop_none({"job_id": job_id, "model": model})
        )


class PromptsResource:
    """``client.prompts.*`` — per-node prompt read/edit + revision history."""

    def __init__(self, client: "Client"):
        self._client = client

    def list(self, job_id: str) -> dict[str, Any]:
        """``GET /prompts/{job_id}`` — every node's current prompt for a job."""
        return self._client.request("GET", f"/prompts/{job_id}")

    def get(self, job_id: str, node_key: str) -> dict[str, Any]:
        """``GET /prompts/{job_id}/{node_key}`` — a single node's full prompt."""
        return self._client.request("GET", f"/prompts/{job_id}/{node_key}")

    def history(self, job_id: str, node_key: str) -> dict[str, Any]:
        """``GET /prompts/{job_id}/{node_key}/history`` — full revision chain."""
        return self._client.request("GET", f"/prompts/{job_id}/{node_key}/history")

    def update(self, job_id: str, node_key: str, prompt: str) -> dict[str, Any]:
        """``POST /prompts/{job_id}/{node_key}`` — set the node's prompt."""
        return self._client.request(
            "POST", f"/prompts/{job_id}/{node_key}", json={"prompt": prompt}
        )


class GtResource:
    """``client.gt.*`` — ground-truth corpus extraction + browsing + search."""

    def __init__(self, client: "Client"):
        self._client = client

    def create(
        self,
        topic: str,
        *,
        queries: list[str] | None = None,
        push_to_github: bool = False,
        target_file: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        """``POST /gt`` — extract ground truths via SearXNG + LLM distillation."""
        body = _drop_none({
            "topic": topic,
            "queries": queries,
            "push_to_github": push_to_github,
            "target_file": target_file,
            "model": model,
        })
        return self._client.request("POST", "/gt", json=body)

    def list(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        include_history: bool = False,
        domain: str | None = None,
    ) -> dict[str, Any]:
        """``GET /gt/list`` — paginated TOON entries; optional domain filter."""
        params = _drop_none({
            "page": page,
            "per_page": per_page,
            "include_history": include_history,
            "domain": domain,
        })
        return self._client.request("GET", "/gt/list", params=params)

    def search(
        self,
        query: str,
        *,
        domain: str | None = None,
        top_k: int = 10,
        include_history: bool = False,
    ) -> dict[str, Any]:
        """``POST /gt/search`` — semantic search across TOON entries."""
        body = _drop_none({
            "query": query,
            "domain": domain,
            "top_k": top_k,
            "include_history": include_history,
        })
        return self._client.request("POST", "/gt/search", json=body)

    def detail(self, entry_id: str) -> dict[str, Any]:
        """``GET /gt/detail/{entry_id}`` — full content of one TOON entry."""
        return self._client.request("GET", f"/gt/detail/{entry_id}")

    def stats(self) -> dict[str, Any]:
        """``GET /gt/stats`` — collection summary (counts per domain, etc.)."""
        return self._client.request("GET", "/gt/stats")


class RagResource:
    """``client.rag.*`` — RAG pipeline query + dedup audit."""

    def __init__(self, client: "Client"):
        self._client = client

    def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        confidence_threshold: float = 0.8,
        skip_rerank: bool = False,
        include_history: bool = False,
        domain: str | None = None,
    ) -> dict[str, Any]:
        """``POST /rag`` — embed → vector + keyword → RRF → rerank → results."""
        body = _drop_none({
            "query": query,
            "top_k": top_k,
            "confidence_threshold": confidence_threshold,
            "skip_rerank": skip_rerank,
            "include_history": include_history,
            "domain": domain,
        })
        return self._client.request("POST", "/rag", json=body)

    def dedup(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        """``GET /rag/dedup`` — paginated near-duplicate rejection log."""
        return self._client.request(
            "GET", "/rag/dedup", params={"limit": limit, "offset": offset}
        )


class ScheduleResource:
    """``client.schedule.*`` — recurring research jobs (cron-driven)."""

    def __init__(self, client: "Client"):
        self._client = client

    def list(self) -> dict[str, Any]:
        """``GET /schedule`` — every saved schedule with its next-run time."""
        return self._client.request("GET", "/schedule")

    def create(
        self,
        topic: str,
        cron_expression: str,
        *,
        depth: str = "medium",
        timezone: str = "UTC",
    ) -> dict[str, Any]:
        """``POST /schedule`` — register a recurring research job."""
        return self._client.request(
            "POST",
            "/schedule",
            json={
                "topic": topic,
                "cron_expression": cron_expression,
                "depth": depth,
                "timezone": timezone,
            },
        )

    def delete(self, schedule_id: int) -> dict[str, Any]:
        """``DELETE /schedule/{schedule_id}`` — remove a schedule."""
        return self._client.request("DELETE", f"/schedule/{schedule_id}")


class ResearchResource:
    """``client.research.*`` — manage saved research sessions.

    The SSE-streamed run-/reply-/PDF-ingest helpers stay on
    ``AsyncClient`` (``aiter_research``, ``aiter_research_reply``,
    ``aiter_research_pdf``); this resource covers the CRUD shape.
    """

    def __init__(self, client: "Client"):
        self._client = client

    def list(
        self,
        *,
        status: str | None = None,
        q: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        """``GET /research/sessions`` — paginated session list."""
        params = _drop_none({"status": status, "q": q, "limit": limit, "offset": offset})
        return self._client.request("GET", "/research/sessions", params=params)

    def find(self, q: str, *, limit: int = 25) -> dict[str, Any]:
        """Convenience: list filtered by topic substring."""
        return self.list(q=q, limit=limit)

    def rename(self, session_id: str, *, topic: str) -> dict[str, Any]:
        """``PATCH /research/sessions/{session_id}`` — set the topic."""
        return self._client.request(
            "PATCH", f"/research/sessions/{session_id}", json={"topic": topic},
        )

    def delete(self, session_id: str) -> dict[str, Any]:
        """``DELETE /research/sessions/{session_id}``. KB entries are NOT removed."""
        return self._client.request("DELETE", f"/research/sessions/{session_id}")


class ModelsResource:
    """``client.models.*`` — read-only inspection of model role assignments
    + Ollama availability.

    `set / reset / probe` are OWUI-only by U.7 design (they mutate per-pipeline
    valves which are session-scoped — to persist, edit MODEL_<role> in .env
    and restart). The read paths derive from `/config` and `/health`.
    """

    def __init__(self, client: "Client"):
        self._client = client

    def list(self) -> dict[str, Any]:
        """Return only the ``model_*`` settings from ``GET /config``.

        Output shape is ``{"fields": [...], "count": <int>}`` mirroring the
        ``/config`` envelope so callers can reuse the same renderer.
        """
        cfg = self._client.request("GET", "/config")
        fields = (cfg or {}).get("fields", []) if isinstance(cfg, dict) else []
        models = [f for f in fields if isinstance(f, dict) and str(f.get("name", "")).startswith("model_")]
        return {"fields": models, "count": len(models)}

    def available(self) -> list[str]:
        """Models currently loaded on the configured Ollama instance.

        Reads ``GET /health`` (no auth needed) and returns the
        ``checks.ollama.models_loaded`` list. Empty list if Ollama is down
        or the field is missing — callers should not rely on length to
        detect failure (use ``client.health()`` for that).
        """
        health = self._client.request("GET", "/health")
        ollama = (health or {}).get("checks", {}).get("ollama", {}) if isinstance(health, dict) else {}
        loaded = ollama.get("models_loaded") if isinstance(ollama, dict) else None
        return list(loaded) if isinstance(loaded, list) else []


class AssistResource:
    """``client.assist.*`` — Assistant Mode (human-in-the-loop) sessions.

    Mirrors ``app/routers/assist.py``. The streaming ``/handoff`` endpoint
    is intentionally not exposed on the sync client — use
    ``AsyncClient.aiter_assist_handoff`` for that one.
    """

    def __init__(self, client: "Client"):
        self._client = client

    def start(
        self,
        job_id: str,
        *,
        handoff_policy: str | None = None,
        replan_policy: str | None = None,
    ) -> dict[str, Any]:
        """``POST /assist/start`` — open an assist session for a job."""
        body = _drop_none({
            "job_id": job_id,
            "handoff_policy": handoff_policy,
            "replan_policy": replan_policy,
        })
        return self._client.request("POST", "/assist/start", json=body)

    def get(self, session_id: str) -> dict[str, Any]:
        """``GET /assist/{session_id}`` — session + step rollup."""
        return self._client.request("GET", f"/assist/{session_id}")

    def next(self, session_id: str) -> dict[str, Any]:
        """``GET /assist/{session_id}/next`` — claim next pending step."""
        return self._client.request("GET", f"/assist/{session_id}/next")

    def submit(
        self,
        session_id: str,
        node_key: str,
        *,
        output: str = "",
        evidence_kind: str = "text",
        evidence_meta: dict[str, Any] | None = None,
        action: str = "submit",
        friction_note: str | None = None,
    ) -> dict[str, Any]:
        """``POST /assist/{session_id}/submit`` — record evidence for a step."""
        body = _drop_none({
            "node_key": node_key,
            "output": output,
            "evidence_kind": evidence_kind,
            "evidence_meta": evidence_meta or {},
            "action": action,
            "friction_note": friction_note,
        })
        return self._client.request("POST", f"/assist/{session_id}/submit", json=body)

    def skip(self, session_id: str, node_key: str) -> dict[str, Any]:
        """Submit with ``action='skip'`` — shorthand for the OWUI ``/assist/skip`` verb."""
        return self.submit(session_id, node_key, action="skip", evidence_kind="none")

    def pause(self, session_id: str) -> dict[str, Any]:
        """``POST /assist/{session_id}/pause``."""
        return self._client.request("POST", f"/assist/{session_id}/pause")

    def resume(self, session_id: str) -> dict[str, Any]:
        """``POST /assist/{session_id}/resume``."""
        return self._client.request("POST", f"/assist/{session_id}/resume")

    def abandon(self, session_id: str) -> dict[str, Any]:
        """``DELETE /assist/{session_id}`` — abandon the session."""
        return self._client.request("DELETE", f"/assist/{session_id}")

    def add_friction(self, session_id: str, node_key: str, note: str) -> dict[str, Any]:
        """``POST /assist/{session_id}/friction`` — append a friction note."""
        return self._client.request(
            "POST", f"/assist/{session_id}/friction",
            json={"node_key": node_key, "note": note},
        )

    def list_friction(self, session_id: str) -> dict[str, Any]:
        """``GET /assist/{session_id}/friction`` — every recorded note."""
        return self._client.request("GET", f"/assist/{session_id}/friction")
