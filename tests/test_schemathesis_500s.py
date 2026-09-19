"""§17.1068 — the two 500 classes schemathesis found on GET routes."""
import pytest
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.main import app


@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_api_key, None)


@pytest.mark.parametrize("path", ["/assist/0", "/assist/0/turns", "/assist/0/steps", "/assist/0/next",
                                  "/assist/not-a-uuid/env", "/assist/0/message/0/tail"])
def test_malformed_session_id_is_a_422_not_a_500(client, path):
    # §17.1131 — was 404 (§17.1068): a malformed id is now rejected by the
    # UuidPath path type before the handler runs, so it is a 422 everywhere.
    r = client.get(path)
    assert r.status_code == 422, (path, r.status_code, r.text[:120])
    assert "pattern" in r.text


def test_delete_on_a_word_that_is_not_a_session_is_a_422(client):
    # schemathesis reached DELETE /assist/{session_id} with "candidates" — §17.1131: 422
    assert client.delete("/assist/candidates").status_code == 422


def test_specs_pending_rejects_a_negative_limit(client):
    assert client.get("/specs/pending?limit=-469").status_code == 422
    assert client.get("/specs/pending?limit=0").status_code == 422


def test_turns_limit_is_bounded(client):
    sid = "613dd1df-4c92-43f7-a35f-c9519add5701"
    assert client.get(f"/assist/{sid}/turns?limit=597511633917476509581312").status_code == 422
    assert client.get(f"/assist/{sid}/turns?limit=0").status_code == 422
