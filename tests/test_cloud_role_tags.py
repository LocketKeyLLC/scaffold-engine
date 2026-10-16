"""§17.1623 — the engine registers its own cloud role tags on its Ollama daemon.

Live (VM 110, 2026-10-16): model_fallback / model_cloud_heavy / model_cloud_alt
named qwen3.5:397b-cloud on a daemon that listed only the local tags — the
deploy skipped cloud tags and the engine only told the operator to pull. These
drive the REAL ensure logic against a fake daemon (no mocked ensure).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app import model_router
from app.config import SWITCHABLE_ROLE_FIELDS, settings
from app.modules import cloud_role_tags as crt

CLOUD = "qwen3.5:397b-cloud"


class FakeDaemon:
    """/api/tags + /api/pull. ``refuse`` maps tag -> the daemon's JSON reply."""

    def __init__(self, tags, refuse=None):
        self.tags = set(tags)
        self.refuse = dict(refuse or {})
        self.pulls: list[str] = []

    async def get(self, url, timeout=None):
        assert url.endswith("/api/tags")
        return httpx.Response(200, json={"models": [{"name": t} for t in sorted(self.tags)]},
                              request=httpx.Request("GET", url))

    async def post(self, url, json=None, timeout=None):
        assert url.endswith("/api/pull") and json["stream"] is False
        self.pulls.append(json["model"])
        if json["model"] in self.refuse:
            return httpx.Response(401, json=self.refuse[json["model"]])
        self.tags.add(json["model"])
        return httpx.Response(200, json={"status": "success"})


@pytest.fixture
def roles(monkeypatch):
    """Every switchable role local + on ollama, then the server's cloud split."""
    for role in SWITCHABLE_ROLE_FIELDS:
        monkeypatch.setattr(settings, role, "qwen3:4b", raising=False)
        monkeypatch.setattr(settings, f"{role}_provider", "ollama", raising=False)
    monkeypatch.setattr(settings, "model_coder", "hf.co/x/coder:Q4")
    for role in ("model_fallback", "model_cloud_heavy", "model_cloud_alt"):
        monkeypatch.setattr(settings, role, CLOUD)
    monkeypatch.setattr(crt, "_state", {"at": 0.0, "failed": {}})


def _daemon(monkeypatch, d: FakeDaemon) -> FakeDaemon:
    monkeypatch.setattr(model_router, "_get_client", lambda: d)
    return d


async def test_only_missing_cloud_role_tags_are_pulled(roles, monkeypatch):
    # a cloud tag on a NON-ollama provider is not the daemon's to pull
    monkeypatch.setattr(settings, "model_triage", "kimi-k3:cloud")
    monkeypatch.setattr(settings, "model_triage_provider", "openai")
    d = _daemon(monkeypatch, FakeDaemon({"qwen3:4b"}))
    res = await crt.ensure_cloud_role_tags(force=True)
    assert d.pulls == [CLOUD], "one pull per distinct cloud tag; local hf.co tag never pulled here"
    assert res == {"pulled": [CLOUD], "failed": {}, "skipped": False}
    assert (await crt.ensure_cloud_role_tags(force=True))["pulled"] == [] and d.pulls == [CLOUD]


async def test_a_refusal_keeps_ollama_s_words_and_health_says_them(roles, monkeypatch):
    from app.health import _model_role_warnings
    reply = {"error": "unauthorized", "signin_url": "https://ollama.com/connect?key=abc"}
    d = _daemon(monkeypatch, FakeDaemon({"qwen3:4b", "hf.co/x/coder:Q4"}, refuse={CLOUD: reply}))
    res = await crt.ensure_cloud_role_tags(force=True)
    assert res["pulled"] == [] and "unauthorized" in res["failed"][CLOUD]
    assert "https://ollama.com/connect?key=abc" in res["failed"][CLOUD]
    warns = _model_role_warnings(d.tags)
    assert len(warns) == 2 and "refused by Ollama: unauthorized" in warns[1]
    # throttled retry: a per-request caller inside the window does not re-pull …
    assert (await crt.ensure_cloud_role_tags())["skipped"] is True and d.pulls == [CLOUD]
    # … and once the key is added, the next forced attempt registers it and clears the record
    d.refuse.clear()
    assert (await crt.ensure_cloud_role_tags(force=True))["pulled"] == [CLOUD]
    assert crt.last_failures() == {} and _model_role_warnings(d.tags) == []


async def test_the_fallback_call_registers_an_unpulled_cloud_tag_and_answers(roles, monkeypatch):
    d = _daemon(monkeypatch, FakeDaemon({"qwen3:4b"}))
    calls: list[str] = []

    async def fake_call(endpoint, payload, model, timeout):
        calls.append(model)
        if model == "qwen3:4b":
            return model_router.ModelResponse(model=model, success=False, error="HTTP 500: boom")
        if model not in d.tags:
            return model_router.ModelResponse(
                model=model, success=False, error=f"HTTP 404: {{\"error\":\"model '{model}' not found\"}}")
        return model_router.ModelResponse(model=model, success=True, text="ok")

    with patch.object(model_router, "_call_ollama", side_effect=fake_call), \
         patch.object(model_router, "_sleep_for_attempt", AsyncMock()):
        resp = await model_router._dispatch_with_retry(
            "/api/generate", {}, "qwen3:4b", fallback=CLOUD, max_retries=1)
    assert resp.success and resp.fallback_used
    assert d.pulls == [CLOUD] and calls == ["qwen3:4b", CLOUD, CLOUD]


async def test_a_missing_local_tag_is_never_pulled_by_the_call_path(roles, monkeypatch):
    d = _daemon(monkeypatch, FakeDaemon(set()))

    async def fake_call(endpoint, payload, model, timeout):
        return model_router.ModelResponse(
            model=model, success=False, error=f"HTTP 404: {{\"error\":\"model '{model}' not found\"}}")

    with patch.object(model_router, "_call_ollama", side_effect=fake_call), \
         patch.object(model_router, "_sleep_for_attempt", AsyncMock()):
        resp = await model_router._dispatch_with_retry(
            "/api/generate", {}, "hf.co/x/coder:Q4", fallback="qwen3:4b", max_retries=2)
    assert not resp.success and d.pulls == []


async def test_the_research_gate_registers_the_cloud_tag_instead_of_422(roles, monkeypatch):
    from app.utils.model_validation import _require_valid_models
    d = _daemon(monkeypatch, FakeDaemon({"qwen3:4b", "hf.co/x/coder:Q4"}))
    await _require_valid_models()  # would 422 on model_fallback / model_cloud_alt before §17.1623
    assert d.pulls == [CLOUD]


async def test_the_research_gate_still_refuses_a_missing_local_tag(roles, monkeypatch):
    from fastapi import HTTPException

    from app.utils.model_validation import _require_valid_models
    d = _daemon(monkeypatch, FakeDaemon({"qwen3:4b", CLOUD}))
    with pytest.raises(HTTPException) as e:
        await _require_valid_models()
    assert e.value.status_code == 422 and d.pulls == []
