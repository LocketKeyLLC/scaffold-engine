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


# ── §17.1131 — the second fuzz pass (GET+DELETE+PATCH, ×10) on the typed routes ──

def test_integer_path_id_above_int32_is_a_422(client):
    # schemathesis: DELETE /schedule/448947330693740494848 → asyncpg "value out of int32 range" 500
    assert client.delete("/schedule/448947330693740494848").status_code == 422


def test_offset_above_int64_is_a_422(client):
    assert client.get("/schedule?offset=18446744073709551615&limit=24").status_code == 422
    assert client.get("/jobs?offset=18446744073709551615").status_code == 422


def test_gt_page_deeper_than_the_vector_store_window_is_a_422(client):
    # schemathesis: /gt/list?page=41171 → Milvus rejects offset+limit > 16384 → 503
    r = client.get("/gt/list?page=41171&include_history=false")
    assert r.status_code == 422 and "page too deep" in r.text


def test_nul_byte_in_a_query_string_is_a_422_not_a_500(client):
    # schemathesis: GET /research/sessions?q=l)\x00v → the NUL reaches SQL → DataError 500
    r = client.get("/research/sessions", params={"q": "l)\x00v"})
    assert r.status_code == 422, (r.status_code, r.text[:160])
    assert "invalid input" in r.text


def test_nul_byte_in_a_body_field_is_a_422_not_a_500(client):
    r = client.patch("/research/sessions/98f09fc4-5888-2ce5-bf5d-e750917fddeb", json={"topic": "l)\x00v"})
    assert r.status_code in (404, 422), (r.status_code, r.text[:160])
    assert r.status_code != 500


def test_dbapi_handler_only_maps_data_errors():
    """A real DB fault (not a DataError) must still propagate to the error logger."""
    import asyncio
    from unittest.mock import MagicMock
    from sqlalchemy.exc import DBAPIError
    from app.main import _dbapi_data_error_handler

    class DataError(Exception): ...
    class OtherError(Exception): ...
    req = MagicMock(); req.url.path = "/x"
    ok = asyncio.run(_dbapi_data_error_handler(req, DBAPIError("s", {}, DataError("invalid input for query argument $1: 'x' (bad)"))))
    assert ok.status_code == 422 and b"invalid input" in ok.body
    with pytest.raises(DBAPIError):
        asyncio.run(_dbapi_data_error_handler(req, DBAPIError("s", {}, OtherError("connection reset"))))
