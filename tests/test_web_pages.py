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
def test_web_research_page_empty(web):
    resp = web.get("/web/research")
    assert resp.status_code == 200
    assert "Research sessions" in resp.text


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
