"""Async resource sub-objects exposed on ``AsyncClient``.

Mirror of ``_resources.py`` — same method names, same signatures, same
URL templates. Only the dispatch verb differs (``await client.request``
instead of ``client.request``). Kept side-by-side rather than abstracted
because every method is 1-3 lines and the signatures must stay loud for
IDE/typing.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from ._resources import _drop_none

if TYPE_CHECKING:
    from .async_client import AsyncClient


class AsyncJobsResource:
    def __init__(self, client: "AsyncClient"):
        self._client = client

    async def list(
        self,
        *,
        status: str | None = None,
        q: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        params = _drop_none({"status": status, "q": q, "limit": limit, "offset": offset})
        return await self._client.request("GET", "/jobs", params=params)

    async def status(self, job_id: str) -> dict[str, Any]:
        return await self._client.request("GET", f"/exec/status/{job_id}")

    async def delete(self, job_id: str) -> dict[str, Any]:
        return await self._client.request("DELETE", f"/jobs/{job_id}")

    async def update(self, job_id: str, *, title: str) -> dict[str, Any]:
        return await self._client.request("PATCH", f"/jobs/{job_id}", json={"title": title})

    async def cleanup(self) -> dict[str, Any]:
        return await self._client.request("POST", "/jobs/cleanup")

    async def retry(self, job_id: str, node_key: str) -> dict[str, Any]:
        return await self._client.request(
            "POST", "/exec/retry", json={"job_id": job_id, "node_key": node_key}
        )


class AsyncDagResource:
    def __init__(self, client: "AsyncClient"):
        self._client = client

    async def get(self, job_id: str) -> dict[str, Any]:
        return await self._client.request("GET", f"/dag/{job_id}")

    async def create(self, job_id: str, *, model: str | None = None) -> dict[str, Any]:
        return await self._client.request(
            "POST", "/dag", json=_drop_none({"job_id": job_id, "model": model})
        )


class AsyncPromptsResource:
    def __init__(self, client: "AsyncClient"):
        self._client = client

    async def list(self, job_id: str) -> dict[str, Any]:
        return await self._client.request("GET", f"/prompts/{job_id}")

    async def get(self, job_id: str, node_key: str) -> dict[str, Any]:
        return await self._client.request("GET", f"/prompts/{job_id}/{node_key}")

    async def history(self, job_id: str, node_key: str) -> dict[str, Any]:
        return await self._client.request("GET", f"/prompts/{job_id}/{node_key}/history")

    async def update(self, job_id: str, node_key: str, prompt: str) -> dict[str, Any]:
        return await self._client.request(
            "POST", f"/prompts/{job_id}/{node_key}", json={"prompt": prompt}
        )


class AsyncGtResource:
    def __init__(self, client: "AsyncClient"):
        self._client = client

    async def create(
        self,
        topic: str,
        *,
        queries: list[str] | None = None,
        push_to_github: bool = False,
        target_file: str | None = None,
        model: str | None = None,
    ) -> dict[str, Any]:
        body = _drop_none({
            "topic": topic,
            "queries": queries,
            "push_to_github": push_to_github,
            "target_file": target_file,
            "model": model,
        })
        return await self._client.request("POST", "/gt", json=body)

    async def list(
        self,
        *,
        page: int = 1,
        per_page: int = 20,
        include_history: bool = False,
        domain: str | None = None,
    ) -> dict[str, Any]:
        params = _drop_none({
            "page": page,
            "per_page": per_page,
            "include_history": include_history,
            "domain": domain,
        })
        return await self._client.request("GET", "/gt/list", params=params)

    async def search(
        self,
        query: str,
        *,
        domain: str | None = None,
        top_k: int = 10,
        include_history: bool = False,
    ) -> dict[str, Any]:
        body = _drop_none({
            "query": query,
            "domain": domain,
            "top_k": top_k,
            "include_history": include_history,
        })
        return await self._client.request("POST", "/gt/search", json=body)

    async def detail(self, entry_id: str) -> dict[str, Any]:
        return await self._client.request("GET", f"/gt/detail/{entry_id}")

    async def stats(self) -> dict[str, Any]:
        return await self._client.request("GET", "/gt/stats")


class AsyncRagResource:
    def __init__(self, client: "AsyncClient"):
        self._client = client

    async def search(
        self,
        query: str,
        *,
        top_k: int = 10,
        confidence_threshold: float = 0.8,
        skip_rerank: bool = False,
        include_history: bool = False,
        domain: str | None = None,
    ) -> dict[str, Any]:
        body = _drop_none({
            "query": query,
            "top_k": top_k,
            "confidence_threshold": confidence_threshold,
            "skip_rerank": skip_rerank,
            "include_history": include_history,
            "domain": domain,
        })
        return await self._client.request("POST", "/rag", json=body)

    async def dedup(self, *, limit: int = 50, offset: int = 0) -> dict[str, Any]:
        return await self._client.request(
            "GET", "/rag/dedup", params={"limit": limit, "offset": offset}
        )


class AsyncScheduleResource:
    def __init__(self, client: "AsyncClient"):
        self._client = client

    async def list(self) -> dict[str, Any]:
        return await self._client.request("GET", "/schedule")

    async def create(
        self,
        topic: str,
        cron_expression: str,
        *,
        depth: str = "medium",
        timezone: str = "UTC",
    ) -> dict[str, Any]:
        return await self._client.request(
            "POST",
            "/schedule",
            json={
                "topic": topic,
                "cron_expression": cron_expression,
                "depth": depth,
                "timezone": timezone,
            },
        )

    async def delete(self, schedule_id: int) -> dict[str, Any]:
        return await self._client.request("DELETE", f"/schedule/{schedule_id}")


class AsyncResearchResource:
    def __init__(self, client: "AsyncClient"):
        self._client = client

    async def list(
        self,
        *,
        status: str | None = None,
        q: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> dict[str, Any]:
        params = _drop_none({"status": status, "q": q, "limit": limit, "offset": offset})
        return await self._client.request("GET", "/research/sessions", params=params)

    async def find(self, q: str, *, limit: int = 25) -> dict[str, Any]:
        return await self.list(q=q, limit=limit)

    async def rename(self, session_id: str, *, topic: str) -> dict[str, Any]:
        return await self._client.request(
            "PATCH", f"/research/sessions/{session_id}", json={"topic": topic},
        )

    async def delete(self, session_id: str) -> dict[str, Any]:
        return await self._client.request("DELETE", f"/research/sessions/{session_id}")


class AsyncModelsResource:
    def __init__(self, client: "AsyncClient"):
        self._client = client

    async def list(self) -> dict[str, Any]:
        cfg = await self._client.request("GET", "/config")
        fields = (cfg or {}).get("fields", []) if isinstance(cfg, dict) else []
        models = [
            f for f in fields
            if isinstance(f, dict) and str(f.get("name", "")).startswith("model_")
        ]
        return {"fields": models, "count": len(models)}

    async def available(self) -> list[str]:
        health = await self._client.request("GET", "/health")
        ollama = (health or {}).get("checks", {}).get("ollama", {}) if isinstance(health, dict) else {}
        loaded = ollama.get("models_loaded") if isinstance(ollama, dict) else None
        return list(loaded) if isinstance(loaded, list) else []


class AsyncAssistResource:
    """Async mirror of ``AssistResource``. SSE ``/handoff`` lives on
    ``AsyncClient.aiter_assist_handoff`` (parallels ``aiter_research`` etc.).
    """

    def __init__(self, client: "AsyncClient"):
        self._client = client

    async def start(
        self,
        job_id: str,
        *,
        handoff_policy: str | None = None,
        replan_policy: str | None = None,
    ) -> dict[str, Any]:
        body = _drop_none({
            "job_id": job_id,
            "handoff_policy": handoff_policy,
            "replan_policy": replan_policy,
        })
        return await self._client.request("POST", "/assist/start", json=body)

    async def get(self, session_id: str) -> dict[str, Any]:
        return await self._client.request("GET", f"/assist/{session_id}")

    async def next(self, session_id: str) -> dict[str, Any]:
        return await self._client.request("GET", f"/assist/{session_id}/next")

    async def submit(
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
        body = _drop_none({
            "node_key": node_key,
            "output": output,
            "evidence_kind": evidence_kind,
            "evidence_meta": evidence_meta or {},
            "action": action,
            "friction_note": friction_note,
        })
        return await self._client.request(
            "POST", f"/assist/{session_id}/submit", json=body,
        )

    async def skip(self, session_id: str, node_key: str) -> dict[str, Any]:
        return await self.submit(session_id, node_key, action="skip", evidence_kind="none")

    async def pause(self, session_id: str) -> dict[str, Any]:
        return await self._client.request("POST", f"/assist/{session_id}/pause")

    async def resume(self, session_id: str) -> dict[str, Any]:
        return await self._client.request("POST", f"/assist/{session_id}/resume")

    async def abandon(self, session_id: str) -> dict[str, Any]:
        return await self._client.request("DELETE", f"/assist/{session_id}")

    async def add_friction(self, session_id: str, node_key: str, note: str) -> dict[str, Any]:
        return await self._client.request(
            "POST", f"/assist/{session_id}/friction",
            json={"node_key": node_key, "note": note},
        )

    async def list_friction(self, session_id: str) -> dict[str, Any]:
        return await self._client.request("GET", f"/assist/{session_id}/friction")
