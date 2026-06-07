"""§17.441 — input-validation hardening from the stress-test findings.

Aggressively probing the live API surfaced two bug classes:

  #2  A deeply-nested JSON body raised ``RecursionError`` during parsing →
      HTTP 500 (should be 422) on every JSON POST endpoint, AND wrote an
      "unrecoverable" ``error_logs`` row each time (tripping the unresolved-
      errors alert watchdog). Fixed by an ``@app.exception_handler`` that
      catches it in Starlette's inner ExceptionMiddleware (before
      ErrorLoggingMiddleware), returning a clean 422 with no error_logs row.

  #3  Free-text fields that feed cloud LLM calls had no ``max_length``, so a
      1 MB ``idea`` was accepted (200) and forwarded to a billed model — the
      only backstop was the 2 MB body cap. Fixed by ``max_length`` bounds.
"""
import pytest
from pydantic import ValidationError
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.main import app
from app.schemas import (
    IdeaInput, PromptOptimizeInput, ResearchInput, ResearchReplyInput,
    ConfirmInput, RagInput, GtInput, GtSearchInput, ScheduleCreate,
    MAX_LLM_TEXT_LEN, MAX_QUERY_LEN,
)


@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_api_key, None)


# ─────────────────────── Fix #2: RecursionError → 422 ───────────────────────

def _deep_body(depth: int = 3000) -> bytes:
    # Depth far exceeds Python's ~1000 default recursion limit, so the JSON
    # parser raises RecursionError before any field validation runs.
    return ("{\"idea\":" + "[" * depth + "\"x\"" + "]" * depth + "}").encode()


def test_deeply_nested_body_returns_422_not_500(client):
    r = client.post("/ideate", content=_deep_body(),
                    headers={"content-type": "application/json"})
    assert r.status_code == 422, r.text
    assert r.json().get("error") == "RecursionError"


def test_deeply_nested_body_422_across_json_post_endpoints(client):
    # The 500 was systemic — confirm the handler covers every JSON POST route.
    for ep in ("/ideate", "/research", "/dag", "/rag", "/design"):
        r = client.post(ep, content=_deep_body(),
                        headers={"content-type": "application/json"})
        assert r.status_code == 422, f"{ep}: {r.status_code} {r.text}"


# ─────────────────── Fix #3: max_length on LLM-feeding fields ───────────────────

_MINIMAL = {
    IdeaInput: {"idea": "x"},
    PromptOptimizeInput: {"prompt": "x"},
    ResearchReplyInput: {"session_id": "s", "reply": "x"},
    ResearchInput: {"topic": "x"},
    ScheduleCreate: {"topic": "x", "cron_expression": "0 9 * * 1"},
    GtInput: {"topic": "x"},
    GtSearchInput: {"query": "x"},
    RagInput: {"query": "x"},
}


@pytest.mark.parametrize("model,field,cap", [
    (IdeaInput, "idea", MAX_LLM_TEXT_LEN),
    (PromptOptimizeInput, "prompt", MAX_LLM_TEXT_LEN),
    (ResearchReplyInput, "reply", MAX_LLM_TEXT_LEN),
    (ResearchInput, "topic", MAX_QUERY_LEN),
    (ScheduleCreate, "topic", MAX_QUERY_LEN),
    (GtInput, "topic", MAX_QUERY_LEN),
    (GtSearchInput, "query", MAX_QUERY_LEN),
    (RagInput, "query", MAX_QUERY_LEN),
])
def test_llm_text_field_over_cap_rejected(model, field, cap):
    kwargs = dict(_MINIMAL[model])
    kwargs[field] = "A" * (cap + 1)
    with pytest.raises(ValidationError):
        model(**kwargs)


@pytest.mark.parametrize("model,field,cap", [
    (IdeaInput, "idea", MAX_LLM_TEXT_LEN),
    (RagInput, "query", MAX_QUERY_LEN),
])
def test_llm_text_field_at_cap_accepted(model, field, cap):
    kwargs = dict(_MINIMAL[model])
    kwargs[field] = "A" * cap
    m = model(**kwargs)
    assert len(getattr(m, field)) == cap


def test_confirm_feedback_optional_but_bounded():
    # None stays valid (optional field) …
    assert ConfirmInput(job_id="j").feedback is None
    # … but an oversized feedback is rejected.
    with pytest.raises(ValidationError):
        ConfirmInput(job_id="j", feedback="A" * (MAX_LLM_TEXT_LEN + 1))


def test_caps_are_generous_enough_for_real_input():
    # Guard against an over-tight cap regressing legitimate use: a 5k-word
    # idea and a paragraph-length research topic must still validate.
    IdeaInput(idea="word " * 5000)          # ~25k chars < 50k
    ResearchInput(topic="background. " * 500)  # ~6k chars < 10k
