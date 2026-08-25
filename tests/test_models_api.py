"""§17.813 — model-management JSON API (audit M13 / plan Phase 4).

Direct-call unit tests: DB + Ollama HTTP are mocked; set_override/clear_override
are patched (their own persistence tests live in test_model_overrides). The
generate-probe path is pinned to NOT route through model_router.generate (whose
smart-fallback would mask a dead tag).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from app.config import SWITCHABLE_ROLE_FIELDS, settings
from app.routers import models as m


# ── GET /models/roles ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_roles_reports_every_role_with_provenance():
    with patch.object(m, "list_overrides",
                      new=AsyncMock(return_value={"model_coder": "kimi-k2.7-code:cloud"})):
        out = await m.get_model_roles(db=AsyncMock())
    by_role = {r["role"]: r for r in out["roles"]}
    assert set(by_role) == {
        "model_router", "model_embedder_pipeline", "model_reranker", "model_coder",
        "model_general", "model_verifier", "model_research_extract",
        "model_cloud_heavy", "model_cloud_alt", "model_fallback", "model_triage",
    }
    assert by_role["model_coder"]["source"] == "override"
    assert by_role["model_coder"]["switchable"] is True
    # the two singletons are locked (config-only — dim-locked embedder,
    # CrossEncoder singleton reranker)
    assert by_role["model_embedder_pipeline"]["switchable"] is False
    assert by_role["model_reranker"]["switchable"] is False
    assert out["switchable"] == sorted(SWITCHABLE_ROLE_FIELDS)
    # every row carries the live model + its env default
    for r in out["roles"]:
        assert r["model"] and "env_default" in r and r["source"] in (
            "override", "env", "default")


# ── PUT /models/roles/{role} ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_put_rejects_non_switchable_role():
    with pytest.raises(HTTPException) as e:
        await m.put_model_role(
            "model_reranker", m.RoleModelInput(model="x"), db=AsyncMock())
    assert e.value.status_code == 422 and "not switchable" in e.value.detail


@pytest.mark.asyncio
async def test_put_rejects_unpulled_tag():
    with patch.object(m, "_pulled_tags",
                      new=AsyncMock(return_value={"qwen3.5:397b-cloud"})), \
         patch.object(m, "set_override", new=AsyncMock()) as so:
        with pytest.raises(HTTPException) as e:
            await m.put_model_role(
                "model_coder", m.RoleModelInput(model="nope:latest"), db=AsyncMock())
    assert e.value.status_code == 422 and "not a pulled" in e.value.detail
    so.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_allows_when_taglist_unreachable():
    """Fail-soft: an unreachable daemon must not brick model management."""
    with patch.object(m, "_pulled_tags", new=AsyncMock(return_value=None)), \
         patch.object(m, "set_override", new=AsyncMock()) as so, \
         patch.object(m, "list_overrides",
                      new=AsyncMock(return_value={"model_coder": "local-model:latest"})):
        out = await m.put_model_role(
            "model_coder", m.RoleModelInput(model="local-model:latest", probe=False),
            db=AsyncMock())
    so.assert_awaited_once()
    assert out["role"] == "model_coder"


@pytest.mark.asyncio
async def test_put_cloud_tag_probe_fail_blocks_set():
    """The §17.632 liveness gotcha: a retired cloud tag still lists (stale 200)
    — the generate-probe must gate the set."""
    with patch.object(m, "_pulled_tags",
                      new=AsyncMock(return_value={"dead-model:cloud"})), \
         patch.object(m, "_generate_probe",
                      new=AsyncMock(return_value={"ok": False, "latency_ms": 40,
                                                  "error": "HTTP 410: retired"})), \
         patch.object(m, "set_override", new=AsyncMock()) as so:
        with pytest.raises(HTTPException) as e:
            await m.put_model_role(
                "model_coder", m.RoleModelInput(model="dead-model:cloud"),
                db=AsyncMock())
    assert e.value.status_code == 422 and "generate probe" in e.value.detail
    so.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_cloud_tag_probe_ok_sets():
    probe = AsyncMock(return_value={"ok": True, "latency_ms": 900, "error": None})
    with patch.object(m, "_pulled_tags",
                      new=AsyncMock(return_value={"kimi-k2.6:cloud"})), \
         patch.object(m, "_generate_probe", new=probe), \
         patch.object(m, "set_override", new=AsyncMock()) as so, \
         patch.object(m, "list_overrides",
                      new=AsyncMock(return_value={"model_verifier": "kimi-k2.6:cloud"})):
        out = await m.put_model_role(
            "model_verifier", m.RoleModelInput(model="kimi-k2.6:cloud"),
            db=AsyncMock())
    probe.assert_awaited_once_with("kimi-k2.6:cloud")
    so.assert_awaited_once()
    assert out["source"] == "override"


@pytest.mark.asyncio
async def test_put_local_tag_skips_probe():
    """Probing a local tag would force a full model load on a CPU-only host —
    only the pulled-list check applies."""
    probe = AsyncMock()
    with patch.object(m, "_pulled_tags",
                      new=AsyncMock(return_value={"llama3.2:3b"})), \
         patch.object(m, "_generate_probe", new=probe), \
         patch.object(m, "set_override", new=AsyncMock()), \
         patch.object(m, "list_overrides",
                      new=AsyncMock(return_value={"model_general": "llama3.2:3b"})):
        await m.put_model_role(
            "model_general", m.RoleModelInput(model="llama3.2:3b"), db=AsyncMock())
    probe.assert_not_awaited()


# ── DELETE /models/roles/{role} ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_clears_override_and_reports_reverted_row():
    with patch.object(m, "clear_override", new=AsyncMock()) as co, \
         patch.object(m, "list_overrides", new=AsyncMock(return_value={})):
        out = await m.delete_model_role("model_coder", db=AsyncMock())
    co.assert_awaited_once()
    assert out["role"] == "model_coder" and out["source"] in ("env", "default")


@pytest.mark.asyncio
async def test_delete_unknown_role_422():
    with pytest.raises(HTTPException) as e:
        await m.delete_model_role("model_reranker", db=AsyncMock())
    assert e.value.status_code == 422


# ── POST /models/probe ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_probe_endpoint_returns_result():
    with patch.object(m, "_generate_probe",
                      new=AsyncMock(return_value={"ok": True, "latency_ms": 1200,
                                                  "error": None})):
        out = await m.probe_model(m.ProbeInput(model="kimi-k2.6:cloud"))
    assert out == {"model": "kimi-k2.6:cloud", "ok": True, "latency_ms": 1200,
                   "error": None}


@pytest.mark.asyncio
async def test_generate_probe_is_direct_and_surfaces_410():
    """The probe is a DIRECT /api/generate call (model_router.generate's
    smart-fallback would answer from the fallback model and mask a dead tag),
    and a retired-cloud 410 comes back as ok=False with the body."""
    from unittest.mock import MagicMock
    resp = MagicMock(status_code=410, text="model retired")
    client = AsyncMock()
    client.post = AsyncMock(return_value=resp)
    acm = MagicMock()
    acm.__aenter__ = AsyncMock(return_value=client)
    acm.__aexit__ = AsyncMock(return_value=False)
    with patch.object(m.httpx, "AsyncClient", return_value=acm):
        out = await m._generate_probe("dead:cloud")
    url = client.post.call_args.args[0]
    assert url.endswith("/api/generate")
    assert client.post.call_args.kwargs["json"]["model"] == "dead:cloud"
    assert out["ok"] is False and "410" in out["error"]


# ── write routes are admin-gated ─────────────────────────────────────────────


def test_write_routes_require_admin():
    from app.authz import require_admin
    gated = {"put_model_role", "delete_model_role", "probe_model"}
    for route in m.router.routes:
        if route.endpoint.__name__ in gated:
            deps = [d.dependency for d in route.dependencies]
            assert require_admin in deps, f"{route.endpoint.__name__} missing require_admin"
            gated.discard(route.endpoint.__name__)
    assert not gated, f"routes not found: {gated}"
