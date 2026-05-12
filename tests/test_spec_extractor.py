"""
Unit tests for ``app.sim.spec_extractor`` — NL → spec extraction.

LLM calls are mocked via ``patch('app.sim.spec_extractor.model_router')``
so these run in milliseconds with no network. The ``make_mock_db``
fixture from ``conftest`` substitutes for the AsyncSession; we assert
INSERTs happen only when validation passes and never otherwise.
"""
from __future__ import annotations

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.providers.base import ModelResponse
from app.sim.spec_extractor import extract_spec
from tests.conftest import make_mock_db


# ---------------------------------------------------------------------------
# Canned LLM payloads
# ---------------------------------------------------------------------------

_VALID_SPEC = {
    "schema_version": "1.0.0",
    "design": {
        "name": "RC low-pass filter",
        "kind": "analog_circuit",
        "description": "First-order passive RC low-pass.",
    },
    "constraints": [
        {
            "id": "fc_3db",
            "kind": "electrical.frequency",
            "description": "-3 dB corner frequency.",
            "target": 1000.0,
            "tolerance_pct": 5.0,
            "unit": "Hz",
            "criticality": "required",
        }
    ],
}


def _llm_response(text: str, *, success: bool = True, error: str | None = None) -> ModelResponse:
    return ModelResponse(
        text=text,
        model="qwen3-vl:235b-instruct-cloud",
        success=success,
        error=error,
    )


def _patch_chat(monkeypatch, response: ModelResponse) -> MagicMock:
    mock = AsyncMock(return_value=response)
    monkeypatch.setattr("app.sim.spec_extractor.model_router.chat", mock)
    return mock


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_extract_spec_success_persists_row(monkeypatch):
    _patch_chat(monkeypatch, _llm_response(json.dumps({"spec": _VALID_SPEC})))
    db = make_mock_db(scalar=uuid.uuid4())

    result = await extract_spec("Build an RC low-pass...", db=db)

    assert result.ok is True
    assert result.spec is not None
    assert result.spec_id is not None
    assert result.ambiguities == []
    assert result.errors == []
    # Exactly one INSERT happened.
    assert db.execute.await_count == 1
    assert db.commit.await_count == 1


@pytest.mark.smoke
async def test_extract_spec_success_with_job_id(monkeypatch):
    _patch_chat(monkeypatch, _llm_response(json.dumps({"spec": _VALID_SPEC})))
    db = make_mock_db(scalar=uuid.uuid4())
    job_id = uuid.uuid4()

    result = await extract_spec("...", db=db, job_id=job_id)

    assert result.ok is True
    # The INSERT param dict carries the job_id as a string (UUID serialized).
    insert_call = db.execute.await_args
    assert insert_call.args[1]["job_id"] == str(job_id)


@pytest.mark.smoke
async def test_extract_spec_strips_markdown_fences(monkeypatch):
    """parse_json_object handles ```json fences; the extractor must
    not silently fail on a fenced LLM response."""
    fenced = "```json\n" + json.dumps({"spec": _VALID_SPEC}) + "\n```"
    _patch_chat(monkeypatch, _llm_response(fenced))
    db = make_mock_db(scalar=uuid.uuid4())

    result = await extract_spec("...", db=db)
    assert result.ok is True


@pytest.mark.smoke
async def test_extract_spec_json_repair_recovers(monkeypatch):
    """parse_json_object falls back to json_repair on minor LLM
    JSON-formatting glitches (trailing commas, single quotes, etc.).
    A trailing comma after the constraints array is the canonical case."""
    glitchy = '{"spec": ' + json.dumps(_VALID_SPEC) + ',}'  # trailing comma
    _patch_chat(monkeypatch, _llm_response(glitchy))
    db = make_mock_db(scalar=uuid.uuid4())

    result = await extract_spec("...", db=db)
    assert result.ok is True, result.errors


# ---------------------------------------------------------------------------
# Ambiguity path — no row written
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_extract_spec_ambiguity_no_db_write(monkeypatch):
    body = {
        "ambiguities": [
            {
                "field": "constraints[0].target",
                "reason": "'Fast' is relative.",
                "question": "What corner frequency in Hz?",
            }
        ]
    }
    _patch_chat(monkeypatch, _llm_response(json.dumps(body)))
    db = make_mock_db()

    result = await extract_spec("Make a fast filter.", db=db)

    assert result.ok is False
    assert result.spec is None
    assert result.spec_id is None
    assert len(result.ambiguities) == 1
    assert result.ambiguities[0].question.startswith("What corner")
    assert result.errors == []
    # No INSERT happened.
    assert db.execute.await_count == 0
    assert db.commit.await_count == 0


@pytest.mark.smoke
async def test_extract_spec_ambiguity_empty_list_falls_through(monkeypatch):
    """``ambiguities: []`` is not a valid rejection — the LLM signalled
    ambiguities then provided none. Treat as parse error rather than
    accidentally writing a row that wasn't there."""
    _patch_chat(monkeypatch, _llm_response(json.dumps({"ambiguities": []})))
    db = make_mock_db()

    result = await extract_spec("...", db=db)
    assert result.ok is False
    assert result.errors  # falls into "neither shape" error
    assert db.execute.await_count == 0


# ---------------------------------------------------------------------------
# Error paths — LLM failure, malformed JSON, schema violation
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_extract_spec_llm_failure_no_db_write(monkeypatch):
    _patch_chat(monkeypatch, _llm_response("", success=False, error="model unreachable"))
    db = make_mock_db()

    result = await extract_spec("...", db=db)

    assert result.ok is False
    assert result.errors
    assert "model unreachable" in result.errors[0]
    assert db.execute.await_count == 0


@pytest.mark.smoke
async def test_extract_spec_empty_response(monkeypatch):
    _patch_chat(monkeypatch, _llm_response("   "))  # whitespace only
    db = make_mock_db()

    result = await extract_spec("...", db=db)

    assert result.ok is False
    assert result.errors
    assert db.execute.await_count == 0


@pytest.mark.smoke
async def test_extract_spec_unparseable_json_no_db_write(monkeypatch):
    _patch_chat(monkeypatch, _llm_response("this is not JSON at all"))
    db = make_mock_db()

    result = await extract_spec("...", db=db)

    assert result.ok is False
    assert any("JSON object" in e for e in result.errors)
    assert db.execute.await_count == 0


@pytest.mark.smoke
async def test_extract_spec_wrong_envelope_shape(monkeypatch):
    """Output that's neither {spec: ...} nor {ambiguities: [...]}."""
    _patch_chat(monkeypatch, _llm_response(json.dumps({"random_key": "value"})))
    db = make_mock_db()

    result = await extract_spec("...", db=db)

    assert result.ok is False
    assert any("neither" in e for e in result.errors)
    assert db.execute.await_count == 0


@pytest.mark.smoke
async def test_extract_spec_invalid_schema_surfaces_validator_errors(monkeypatch):
    """LLM produced a {spec: {...}} envelope but the inner spec fails
    validation (missing unit). The extractor must propagate the
    validator errors verbatim and refuse to persist."""
    bad_spec = {
        "schema_version": "1.0.0",
        "design": {
            "name": "X",
            "kind": "analog_circuit",
            "description": "X",
        },
        "constraints": [
            {
                "id": "fc",
                "kind": "electrical.frequency",
                "description": "fc.",
                "target": 1000.0,
                # 'unit' deliberately missing → schema violation
                "criticality": "required",
            }
        ],
    }
    _patch_chat(monkeypatch, _llm_response(json.dumps({"spec": bad_spec})))
    db = make_mock_db()

    result = await extract_spec("...", db=db)

    assert result.ok is False
    assert result.errors
    assert any("unit" in e for e in result.errors)
    assert db.execute.await_count == 0


# ---------------------------------------------------------------------------
# Input contract
# ---------------------------------------------------------------------------

@pytest.mark.smoke
async def test_extract_spec_rejects_empty_input():
    db = make_mock_db()
    with pytest.raises(ValueError):
        await extract_spec("", db=db)
    with pytest.raises(ValueError):
        await extract_spec("   \n\t", db=db)


@pytest.mark.smoke
async def test_extract_spec_uses_default_role_from_settings(monkeypatch):
    """Default ``model_role`` resolves from ``settings.spec_extractor_model_role``
    rather than a hardcoded constant."""
    from app.config import settings as live_settings
    captured_role: dict[str, str] = {}

    async def fake_chat(messages, *, role, temperature, max_tokens):
        captured_role["role"] = role
        return _llm_response(json.dumps({"spec": _VALID_SPEC}))

    monkeypatch.setattr("app.sim.spec_extractor.model_router.chat", fake_chat)
    monkeypatch.setattr(live_settings, "spec_extractor_model_role", "model_verifier")
    db = make_mock_db(scalar=uuid.uuid4())

    await extract_spec("...", db=db)
    assert captured_role["role"] == "model_verifier"


@pytest.mark.smoke
async def test_extract_spec_explicit_role_overrides_settings(monkeypatch):
    captured_role: dict[str, str] = {}

    async def fake_chat(messages, *, role, temperature, max_tokens):
        captured_role["role"] = role
        return _llm_response(json.dumps({"spec": _VALID_SPEC}))

    monkeypatch.setattr("app.sim.spec_extractor.model_router.chat", fake_chat)
    db = make_mock_db(scalar=uuid.uuid4())

    await extract_spec("...", db=db, model_role="model_coder")
    assert captured_role["role"] == "model_coder"
