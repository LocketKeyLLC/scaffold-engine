"""§17.480 (Slice 3) — new web surfaces: RAG search / model / research."""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.database import get_db


def _empty_db():
    res = MagicMock()
    res.mappings.return_value.all.return_value = []
    res.mappings.return_value.first.return_value = None
    res.first.return_value = None
    db = AsyncMock()
    db.execute = AsyncMock(return_value=res)
    return db


@pytest.fixture
def web():
    app.dependency_overrides[get_db] = _empty_db
    try:
        with TestClient(app, follow_redirects=False) as tc:
            yield tc
    finally:
        app.dependency_overrides.pop(get_db, None)


@pytest.mark.smoke
def test_web_model_page(web):
    resp = web.get("/web/model")
    assert resp.status_code == 200
    assert "Model configuration" in resp.text
    assert "general" in resp.text  # a role row


@pytest.mark.smoke
def test_web_model_page_set_forms_and_locked(web):
    # §17.483 — page now exposes set forms for switchable roles (incl. the
    # previously-missing cloud_heavy/cloud_alt) and a config-locked marker for
    # the embedder/reranker singletons.
    resp = web.get("/web/model")
    assert resp.status_code == 200
    body = resp.text
    assert 'name="role" value="model_general"' in body
    assert "cloud_heavy" in body and "cloud_alt" in body
    assert "config-locked" in body  # embedder/reranker not settable
    assert 'action="/web/model"' in body  # POST form present


@pytest.mark.smoke
def test_web_model_set_success(web):
    # Valid role + a tag Ollama confirms → mutates settings, PRG-redirects.
    from app.config import settings
    original = settings.model_general
    try:
        with patch("app.web.routes._ollama_tag_exists",
                   new=AsyncMock(return_value=True)):
            resp = web.post("/web/model",
                            data={"role": "model_general", "model": "newmodel:7b"})
        assert resp.status_code == 302
        assert resp.headers["location"] == "/web/model?set=model_general"
        assert settings.model_general == "newmodel:7b"
    finally:
        settings.model_general = original


@pytest.mark.smoke
def test_web_model_set_rejects_unknown_tag(web):
    # Ollama reachable but the tag isn't pulled → reject, settings untouched.
    from app.config import settings
    original = settings.model_coder
    try:
        with patch("app.web.routes._ollama_tag_exists",
                   new=AsyncMock(return_value=False)):
            resp = web.post("/web/model",
                            data={"role": "model_coder", "model": "ghost:1b"})
        assert resp.status_code == 302
        assert "error=" in resp.headers["location"]
        assert settings.model_coder == original  # unchanged
    finally:
        settings.model_coder = original


@pytest.mark.smoke
def test_web_model_set_rejects_locked_role(web):
    # A config-locked singleton (reranker) is rejected by set_runtime_model
    # even when the tag validates. Tag-check is allowed (None=unreachable).
    from app.config import settings
    original = settings.model_reranker
    with patch("app.web.routes._ollama_tag_exists",
               new=AsyncMock(return_value=None)):
        resp = web.post("/web/model",
                        data={"role": "model_reranker", "model": "x:1b"})
    assert resp.status_code == 302
    assert "error=" in resp.headers["location"]
    assert settings.model_reranker == original


@pytest.mark.smoke
def test_web_model_set_failsoft_when_ollama_unreachable(web):
    # Ollama unreachable (tag-check None) → allow the set rather than block.
    from app.config import settings
    original = settings.model_fallback
    try:
        with patch("app.web.routes._ollama_tag_exists",
                   new=AsyncMock(return_value=None)):
            resp = web.post("/web/model",
                            data={"role": "model_fallback", "model": "offline:9b"})
        assert resp.status_code == 302
        assert resp.headers["location"] == "/web/model?set=model_fallback"
        assert settings.model_fallback == "offline:9b"
    finally:
        settings.model_fallback = original


@pytest.mark.smoke
def test_web_model_page_shows_override_and_reset(web):
    # §17.484 — a role whose live value differs from its env default renders
    # the override badge, the .env default, and a reset form.
    from app.config import settings, env_default_model
    original = settings.model_coder
    settings.model_coder = "overridden:1b"
    try:
        resp = web.get("/web/model")
        assert resp.status_code == 200
        assert "override" in resp.text
        assert 'action="/web/model/reset"' in resp.text
        assert env_default_model("model_coder") in resp.text  # default shown
    finally:
        settings.model_coder = original


@pytest.mark.smoke
def test_web_model_reset_reverts_to_env_default(web):
    # §17.484 — reset clears the override; settings revert to the env default.
    from app.config import settings, env_default_model
    env_def = env_default_model("model_general")
    settings.model_general = "temp:9b"
    try:
        resp = web.post("/web/model/reset", data={"role": "model_general"})
        assert resp.status_code == 302
        assert resp.headers["location"] == "/web/model?reset=model_general"
        assert settings.model_general == env_def
    finally:
        settings.model_general = env_def


@pytest.mark.smoke
def test_web_model_reset_rejects_locked_role(web):
    from app.config import settings
    original = settings.model_reranker
    resp = web.post("/web/model/reset", data={"role": "model_reranker"})
    assert resp.status_code == 302
    assert "error=" in resp.headers["location"]
    assert settings.model_reranker == original


@pytest.mark.smoke
def test_web_research_page_empty(web):
    resp = web.get("/web/research")
    assert resp.status_code == 200
    assert "research-launch" in resp.text  # §17.481 launch form present


@pytest.mark.smoke
def test_web_rag_page_no_query(web):
    resp = web.get("/web/rag")
    assert resp.status_code == 200
    assert "Knowledge base search" in resp.text


@pytest.mark.smoke
def test_web_rag_page_with_results(web):
    fake = {"status": "ok", "results": [
        {"title": "RC filter", "content": "a passive low-pass…",
         "domain": "eng", "confidence_score": 0.91, "source_url": None},
    ]}
    with patch("app.modules.rag_pipeline.query_rag", new=AsyncMock(return_value=fake)):
        resp = web.get("/web/rag?q=filter")
    assert resp.status_code == 200
    assert "RC filter" in resp.text and "rag-result" in resp.text


# §17.481 — web research launch + detail.
def _db_with_row(row):
    res = MagicMock()
    res.mappings.return_value.first.return_value = row
    res.mappings.return_value.all.return_value = [row] if row else []
    res.first.return_value = row
    db = AsyncMock()
    db.execute = AsyncMock(return_value=res)
    return db


_SESSION = {
    "id": "01ab243e-1234-5678-9abc-def012345678", "topic": "Quantum ECC",
    "depth": "deep", "domain": None, "status": "running", "summary": None,
    "error_message": None, "iterations_completed": 2,
    "total_entries_extracted": 5, "total_entries_ingested": 4,
    "total_entries_rejected": 1, "total_urls_searched": 7, "total_queries": 3,
    "coverage_pct": 40, "duration_ms": 12000,
    "created_at": "2026-06-12", "completed_at": None,
}


@pytest.mark.smoke
def test_web_research_launch_spawns(web):
    with patch("app.modules.research_agent.spawn_research_background") as sp:
        resp = web.post("/web/research",
                        data={"topic": "quantum error correction", "depth": "deep"})
    assert resp.status_code == 302
    sp.assert_called_once()
    assert sp.call_args.args[0] == "quantum error correction"
    assert sp.call_args.kwargs.get("depth") == "deep"


@pytest.mark.smoke
def test_web_research_launch_empty_noop(web):
    with patch("app.modules.research_agent.spawn_research_background") as sp:
        resp = web.post("/web/research", data={"topic": "   ", "depth": "medium"})
    assert resp.status_code == 302
    sp.assert_not_called()


@pytest.mark.smoke
def test_web_research_detail_renders():
    app.dependency_overrides[get_db] = lambda: _db_with_row(_SESSION)
    try:
        with TestClient(app, follow_redirects=False) as tc:
            resp = tc.get("/web/research/" + _SESSION["id"])
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert resp.status_code == 200
    assert "research-detail-root" in resp.text and "Progress" in resp.text
    assert "Quantum ECC" in resp.text


@pytest.mark.smoke
def test_web_research_detail_bad_uuid_400(web):
    resp = web.get("/web/research/not-a-uuid")
    assert resp.status_code == 400
