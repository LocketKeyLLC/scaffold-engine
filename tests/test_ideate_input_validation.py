"""§17.820 (plan 5.9) — ideation input validation ported from the /web form.

The retired /web form pre-validated idea/domain before calling the API; the
JSON API itself did NOT: a blank idea created a real job row, and an unknown
domain raised a bare ValueError out of create_ideation_job → 500. The gate
now lives on IdeaInput (min_length + validators), so every consumer
(/ideate, /ideate/start, /decompose) inherits it.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.auth import require_api_key
from app.main import app
from app.schemas import IdeaInput, ResearchInput


@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        with TestClient(app, follow_redirects=False) as tc:
            yield tc
    finally:
        app.dependency_overrides.pop(require_api_key, None)


class TestIdeaInputSchema:
    def test_empty_idea_rejected(self):
        with pytest.raises(ValidationError):
            IdeaInput(idea="")

    def test_whitespace_idea_rejected(self):
        with pytest.raises(ValidationError, match="non-empty"):
            IdeaInput(idea="   \n\t ")

    def test_unknown_domain_rejected(self):
        with pytest.raises(ValidationError, match="domain must be one of"):
            IdeaInput(idea="build a thing", domain="nonsense")

    def test_retrieval_only_domain_rejected(self):
        """code/qa are VALID_DOMAINS (retrieval partitions) but NOT ideation
        domains — create_ideation_job would raise on them, so the schema must
        refuse them up front instead of letting them 500."""
        with pytest.raises(ValidationError, match="domain must be one of"):
            IdeaInput(idea="build a thing", domain="code")

    def test_empty_string_domain_rejected(self):
        """The /web form mapped ""→None; JSON callers say null (the SPA sends
        `domain.value || null`). An explicit "" is an error, not auto-detect."""
        with pytest.raises(ValidationError):
            IdeaInput(idea="build a thing", domain="")

    def test_none_domain_means_auto_detect(self):
        assert IdeaInput(idea="build a thing", domain=None).domain is None

    @pytest.mark.parametrize("domain", ["prompt", "rag", "llm", "spec", "eng", "eng_design"])
    def test_all_ideation_domains_accepted(self, domain):
        assert IdeaInput(idea="build a thing", domain=domain).domain == domain


class TestIdeateEndpointValidation:
    """Endpoint-level: body validation 422s before the endpoint (and any DB
    work) runs — the exact scenarios the /web form used to shield."""

    def test_ideate_start_empty_idea_422(self, client):
        resp = client.post("/ideate/start", json={"idea": ""})
        assert resp.status_code == 422

    def test_ideate_start_whitespace_idea_422(self, client):
        resp = client.post("/ideate/start", json={"idea": "   "})
        assert resp.status_code == 422

    def test_ideate_start_unknown_domain_422_not_500(self, client):
        resp = client.post("/ideate/start", json={"idea": "x", "domain": "bogus"})
        assert resp.status_code == 422
        assert "domain must be one of" in resp.text

    def test_decompose_inherits_the_same_gate(self, client):
        resp = client.post("/decompose", json={"idea": " ", "domain": "bogus"})
        assert resp.status_code == 422


class TestResearchTopicValidation:
    def test_blank_topic_rejected(self):
        """A whitespace topic used to start a real 20-60 min session."""
        with pytest.raises(ValidationError, match="non-empty"):
            ResearchInput(topic="  ")

    def test_research_endpoint_blank_topic_422(self, client):
        resp = client.post("/research", json={"topic": " "})
        assert resp.status_code == 422


class TestMetaDomains:
    def test_meta_domains_serves_the_ideation_allowlist(self, client):
        """§17.820 — /meta/domains derives from idea_refinement.ALLOWED_DOMAINS
        (was a hardcoded twin; the parity test in test_domain_filtering.py
        guards the picker-order tuple)."""
        from app.modules.idea_refinement import ALLOWED_DOMAINS

        resp = client.get("/meta/domains")
        assert resp.status_code == 200
        assert set(resp.json()["domains"]) == set(ALLOWED_DOMAINS)


class TestConfirmFeedbackNormalization:
    @pytest.mark.asyncio
    async def test_whitespace_feedback_becomes_none(self):
        """Ported from the /web confirm form: '  ' means no feedback — it must
        not be folded into the brief as user guidance."""
        from app.authz import Principal
        from app.routers import workflow as w
        from app.schemas import ConfirmInput

        captured = {}

        async def _fake_rac(job_id, db, user_feedback=None, push_to_github=False,
                            model_overrides=None):
            captured["feedback"] = user_feedback
            return {"status": "ok"}

        with patch.object(w, "research_and_compile", _fake_rac), \
             patch.object(w, "assert_visible", new=AsyncMock()), \
             patch.object(w, "resolve_job_overrides", new=AsyncMock(return_value=None)), \
             patch.object(w, "_require_valid_models", new=AsyncMock()):
            body = ConfirmInput(job_id="123e4567-e89b-12d3-a456-426614174000",
                                feedback="   \n ")
            out = await w.ideate_confirm_endpoint(
                body, db=AsyncMock(),
                principal=Principal(identity="admin", role="admin"),
            )
        assert out == {"status": "ok"}
        assert captured["feedback"] is None
