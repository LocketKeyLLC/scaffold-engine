"""Tests for the /config endpoint (Sprint U.5).

The endpoint exposes the orchestrator's loaded Settings with sensitive
fields redacted. We verify three things:
  - Every Settings field is included in the response.
  - Sensitive fields (SecretStr, name-based, URL-with-credentials) are
    redacted to (set)/(unset).
  - Innocent URL-shaped fields (milvus_uri, redis_url, etc.) are NOT
    over-redacted as a side effect of the URL-credentials regex.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.auth import require_api_key
from app.main import app, _is_secret_field


@pytest.fixture
def client():
    app.dependency_overrides[require_api_key] = lambda: "test"
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(require_api_key, None)


def test_config_endpoint_returns_every_field(client):
    """Every Settings field must appear in /config — silent omissions
    would mean operators can't see all the knobs the system reads."""
    from app.config import Settings
    resp = client.get("/config")
    assert resp.status_code == 200
    data = resp.json()
    field_names = {f["name"] for f in data["fields"]}
    expected = set(Settings.model_fields.keys())
    assert field_names == expected, (
        f"missing in response: {expected - field_names}; "
        f"extra in response: {field_names - expected}"
    )


def test_config_default_factory_fields_report_real_default(client):
    """§17.603 — default_factory fields (tool_call_coax_models,
    alert_kind_cooldowns, node_escalation_order) must report their PRODUCED
    default, not 'PydanticUndefined'. finfo.default is undefined for factory
    fields, which also made is_default always False (reported as overridden
    even on the built-in default)."""
    from app.config import Settings
    factory_fields = [
        n for n, fi in Settings.model_fields.items()
        if fi.default_factory is not None
    ]
    assert factory_fields, "expected >=1 default_factory Settings field"
    fields = {f["name"]: f for f in client.get("/config").json()["fields"]}
    for name in factory_fields:
        assert "PydanticUndefined" not in str(fields[name]["default"]), (
            name, fields[name]["default"]
        )
        assert isinstance(fields[name]["is_default"], bool)


def test_config_endpoint_redacts_secret_str_fields(client):
    """SecretStr-typed fields (scaffold_api_key, openai_api_key) must
    never expose their value."""
    resp = client.get("/config")
    fields = {f["name"]: f for f in resp.json()["fields"]}
    for name in ("scaffold_api_key", "openai_api_key"):
        assert fields[name]["value"] in ("(set)", "(unset)"), (
            f"{name} should be redacted, got: {fields[name]['value']!r}"
        )


def test_config_endpoint_redacts_token_named_fields(client):
    """github_token is a plain str but the name-based redaction catches
    it (token-keyword in the name)."""
    resp = client.get("/config")
    fields = {f["name"]: f for f in resp.json()["fields"]}
    assert fields["github_token"]["value"] in ("(set)", "(unset)")


def test_config_endpoint_does_not_overredact_innocent_urls(client):
    """milvus_uri, redis_url, searxng_url, ollama_base_url, openai_base_url
    are credential-free URLs that should be exposed verbatim."""
    resp = client.get("/config")
    fields = {f["name"]: f for f in resp.json()["fields"]}
    for name in ("milvus_uri", "redis_url", "searxng_url",
                 "ollama_base_url", "openai_base_url"):
        value = fields[name]["value"]
        assert "(set)" not in str(value) and "(unset)" not in str(value), (
            f"{name} was wrongly redacted: {value!r}"
        )


def test_is_secret_field_handles_secret_str():
    from pydantic import SecretStr
    assert _is_secret_field("anything", SecretStr("hidden"))


def test_is_secret_field_keyword_matches_name():
    assert _is_secret_field("scaffold_api_key", "value")
    assert _is_secret_field("OPENAI_API_KEY", "value")
    assert _is_secret_field("github_token", "value")
    assert _is_secret_field("postgres_password", "value")
    assert _is_secret_field("webui_secret_key", "value")


def test_is_secret_field_url_with_credentials_is_redacted():
    """database_url has the form scheme://user:pass@host/db — the
    URL-credentials pattern catches it without flagging clean URLs."""
    assert _is_secret_field(
        "database_url",
        "postgresql+asyncpg://scaffold:secret123@scaffold-postgres:5432/db",
    )


def test_is_secret_field_clean_urls_pass_through():
    assert not _is_secret_field("milvus_uri", "http://milvus-standalone:19530")
    assert not _is_secret_field("ollama_base_url", "http://172.18.0.1:11434")
    assert not _is_secret_field("openai_base_url", "https://api.openai.com/v1")


def test_is_secret_field_non_string_non_secret_passes_through():
    assert not _is_secret_field("max_retries", 3)
    assert not _is_secret_field("scheduler_enabled", True)
    assert not _is_secret_field("rerank_max_candidates", 32)


def test_config_response_marks_overridden_fields(client):
    """is_default flag should reflect whether the runtime value matches
    the Settings field default."""
    resp = client.get("/config")
    fields = {f["name"]: f for f in resp.json()["fields"]}
    # log_level defaults to "info"; if env hasn't overridden it, is_default=True.
    assert "log_level" in fields
    assert isinstance(fields["log_level"]["is_default"], bool)
