"""§17.820 (plan 5.9) — detached research kickoff + session detail read.

The retired /web research form was the ONLY caller of
spawn_research_background (fire-and-forget: the 20-60 min run survives the
browser closing — the streaming POST /research instead cancels the session on
client disconnect). POST /research/start ports that capability to JSON, and
GET /research/sessions/{id} ports the detail page's 17-column read (the list
endpoint is thin; /research/verify/{id} is a different payload).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.authz import Principal
from app.main import app
from app.routers import research as r


@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        with TestClient(app, follow_redirects=False) as tc:
            yield tc
    finally:
        app.dependency_overrides.pop(require_api_key, None)


_ADMIN = Principal(identity="admin", role="admin")


class TestResearchStart:
    @pytest.mark.asyncio
    async def test_spawns_detached_with_owner(self):
        from app.schemas import ResearchInput

        with patch("app.modules.research_agent.spawn_research_background") as spawn, \
             patch.object(r, "_require_valid_models", new=AsyncMock()):
            out = await r.research_start_endpoint(
                ResearchInput(topic="  llm eval harnesses ", depth="shallow"),
                principal=Principal(identity="alice", role="user"),
            )
        assert out["status"] == "started"
        assert out["topic"] == "llm eval harnesses"  # stripped
        spawn.assert_called_once()
        kwargs = spawn.call_args.kwargs
        assert spawn.call_args.args[0] == "llm eval harnesses"
        assert kwargs["depth"] == "shallow"
        assert kwargs["owner"] == "alice"

    def test_blank_topic_422_no_spawn(self, client):
        with patch("app.modules.research_agent.spawn_research_background") as spawn:
            resp = client.post("/research/start", json={"topic": "   "})
        assert resp.status_code == 422
        spawn.assert_not_called()

    def test_bad_depth_422(self, client):
        resp = client.post("/research/start", json={"topic": "x", "depth": "bottomless"})
        assert resp.status_code == 422


class TestResearchSessionDetail:
    @pytest.mark.asyncio
    async def test_returns_the_detail_columns(self):
        row = {
            "id": "123e4567-e89b-12d3-a456-426614174000", "topic": "t",
            "depth": "medium", "domain": None, "status": "completed",
            "summary": "s", "error_message": None, "iterations_completed": 2,
            "total_entries_extracted": 10, "total_entries_ingested": 8,
            "total_entries_rejected": 2, "total_urls_searched": 30,
            "total_queries": 6, "coverage_pct": 80.0, "duration_ms": 1234,
            "created_at": "2026-08-25T00:00:00Z", "completed_at": "2026-08-25T00:20:00Z",
        }
        db = AsyncMock()
        db.execute.return_value = MagicMock(
            mappings=MagicMock(return_value=MagicMock(first=MagicMock(return_value=row)))
        )
        out = await r.get_research_session(
            "123e4567-e89b-12d3-a456-426614174000", db=db, principal=_ADMIN)
        assert out == row

    @pytest.mark.asyncio
    async def test_bad_uuid_422(self):
        with pytest.raises(HTTPException) as e:
            await r.get_research_session("not-a-uuid", db=AsyncMock(), principal=_ADMIN)
        assert e.value.status_code == 422

    @pytest.mark.asyncio
    async def test_missing_session_404(self):
        db = AsyncMock()
        db.execute.return_value = MagicMock(
            mappings=MagicMock(return_value=MagicMock(first=MagicMock(return_value=None)))
        )
        with pytest.raises(HTTPException) as e:
            await r.get_research_session(
                "123e4567-e89b-12d3-a456-426614174000", db=db, principal=_ADMIN)
        assert e.value.status_code == 404


class TestSpawnOwnerThreading:
    def test_spawn_signature_accepts_owner(self):
        """§17.820 — owner threads through spawn → run_research_in_background →
        run_research so detached sessions get owner-stamped like SSE ones."""
        import inspect

        from app.modules.research_agent import (
            run_research_in_background,
            spawn_research_background,
        )
        assert "owner" in inspect.signature(spawn_research_background).parameters
        assert "owner" in inspect.signature(run_research_in_background).parameters
