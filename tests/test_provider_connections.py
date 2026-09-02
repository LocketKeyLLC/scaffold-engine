"""§17.900 — universal provider connections: credentials, routing, HF provider.

The gap this closes: three providers (ollama/openai/anthropic) already worked
and config already had per-role provider routing, but nothing could configure
any of it at runtime. `model_overrides` stored (role, model) with no provider,
`PUT /models/roles/{role}` rejected every tag absent from the pulled Ollama list
(so `gpt-5` and `claude-opus-5` were unsettable), and credentials were env-only.

Covered here:
1. `app.utils.secrets` — encryption round-trip, the mask vocabulary, and the
   fail-soft path when a stored ciphertext can no longer be decrypted.
2. `app.config` — runtime provider switching + its validation.
3. The HuggingFace provider's registration and capability flags.
4. `provider_connections` — the settings mirror, masking, and env fallback.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── 1. secrets ───────────────────────────────────────────────────────────────

def test_encrypt_decrypt_round_trip():
    from app.utils import secrets
    secrets.reset_cache()
    token = secrets.encrypt("sk-proj-abc123")
    assert token != "sk-proj-abc123"          # actually encrypted
    assert token.startswith("gAAAAA")          # Fernet envelope
    assert secrets.decrypt(token) == "sk-proj-abc123"


def test_ciphertext_is_not_deterministic():
    """Fernet embeds a random IV — two encryptions of the same key must differ,
    so a DB reader cannot tell that two providers share a credential."""
    from app.utils import secrets
    secrets.reset_cache()
    assert secrets.encrypt("same") != secrets.encrypt("same")


def test_decrypt_returns_none_on_garbage_never_raises():
    """A rotated derivation secret or a corrupted row must degrade to
    'reconnect this provider', never to a 500 or a startup crash."""
    from app.utils import secrets
    secrets.reset_cache()
    assert secrets.decrypt("not-a-fernet-token") is None
    assert secrets.decrypt("") is None
    assert secrets.decrypt(None) is None


def test_rotating_the_secret_makes_old_ciphertext_unreadable():
    from app.utils import secrets
    from pydantic import SecretStr
    secrets.reset_cache()
    with patch("app.config.settings.scaffold_secret_key", SecretStr("secret-one")):
        token = secrets.encrypt("hunter2")
        assert secrets.decrypt(token) == "hunter2"
    secrets.reset_cache()
    with patch("app.config.settings.scaffold_secret_key", SecretStr("secret-two")):
        assert secrets.decrypt(token) is None      # fail-soft, not an exception
    secrets.reset_cache()


def test_mask_is_the_only_representation():
    from app.utils import secrets
    assert secrets.mask("sk-proj-abcdef123456") == "(set)"
    assert secrets.mask("") == "(unset)"
    assert secrets.mask(None) == "(unset)"
    assert secrets.mask("   ") == "(unset)"
    # No prefix, no length, no hint — both leak.
    assert "sk-" not in secrets.mask("sk-proj-abcdef123456")


# ── 2. runtime provider switching ────────────────────────────────────────────

def test_set_and_clear_runtime_provider():
    from app import config
    original = config.settings.model_general_provider
    try:
        config.set_runtime_provider("model_general", "anthropic")
        assert config.settings.model_general_provider == "anthropic"
        config.clear_runtime_provider("model_general")
        assert config.settings.model_general_provider == "ollama"
    finally:
        config.settings.model_general_provider = original


def test_unknown_provider_is_rejected():
    from app import config
    with pytest.raises(ValueError, match="unknown provider"):
        config.set_runtime_provider("model_general", "anthrpoic")   # typo
    with pytest.raises(ValueError, match="unknown provider"):
        config.set_runtime_provider("model_general", "")


def test_singleton_roles_stay_config_only():
    """The embedder dim is locked and the reranker is a process singleton —
    §17.483's rule must hold for the provider half too."""
    from app import config
    with pytest.raises(ValueError, match="config-only"):
        config.set_runtime_provider("model_reranker", "openai")


def test_unknown_role_is_rejected():
    from app import config
    with pytest.raises(ValueError, match="unknown role"):
        config.set_runtime_provider("model_nonexistent", "ollama")


def test_valid_provider_names_tracks_the_literal():
    """The allowlist is read off ProviderName so it can never drift from the
    type that validates .env at boot."""
    from app.config import valid_provider_names
    names = valid_provider_names()
    assert set(names) == {"ollama", "openai", "anthropic", "huggingface"}


def test_provider_field_for():
    from app.config import provider_field_for
    assert provider_field_for("model_general") == "model_general_provider"


# ── 3. the HuggingFace provider ──────────────────────────────────────────────

def test_huggingface_is_registered_with_correct_capabilities():
    from app.providers import available_providers, get_provider
    assert "huggingface" in available_providers()
    p = get_provider("huggingface")
    assert p.supports_chat and p.supports_streaming and p.supports_native_tools
    # Deliberately False: the embedder is a locked 512-dim singleton, and HF's
    # embeddings coverage is model-dependent — advertising it would let an
    # operator bind the embedder to a backend that silently changes the dim.
    assert p.supports_embeddings is False


def test_huggingface_embedder_binding_is_refused():
    from app.providers import ProviderCapabilityError, provider_for_role
    with pytest.raises(ProviderCapabilityError):
        provider_for_role("model_embedder_pipeline",
                          {"model_embedder_pipeline_provider": "huggingface"})


def test_missing_hf_key_names_both_escape_hatches():
    """The error has to tell the operator how to fix it — including that a
    DOWNLOADED HF model is an Ollama pull, not this provider."""
    from pydantic import SecretStr
    from app.providers.base import ProviderUnavailableError
    from app.providers.huggingface import HuggingFaceProvider
    with patch("app.config.settings.huggingface_api_key", SecretStr("")):
        with pytest.raises(ProviderUnavailableError) as exc:
            HuggingFaceProvider._auth_headers()
    msg = str(exc.value)
    assert "Settings → Connections" in msg
    assert "ollama pull hf.co/" in msg


def test_default_provider_is_the_fallback():
    """provider_for_role falls back to model_default_provider, so 'move
    everything to Claude' is one switch rather than eleven."""
    from app.providers import provider_for_role
    with patch("app.config.settings.model_general_provider", None), \
         patch("app.config.settings.model_default_provider", "anthropic"):
        assert provider_for_role("model_general").name == "anthropic"


# ── 4. connections ───────────────────────────────────────────────────────────

def _db(rows=None):
    db = MagicMock()
    res = MagicMock()
    res.mappings.return_value.all.return_value = rows or []
    res.scalar.return_value = None
    db.execute = AsyncMock(return_value=res)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_list_connections_masks_and_reports_source():
    from pydantic import SecretStr
    from app.modules import provider_connections as pc
    db = _db()
    with patch("app.config.settings.openai_api_key", SecretStr("sk-from-env")), \
         patch("app.config.settings.anthropic_api_key", SecretStr("")):
        conns = {c["provider"]: c for c in await pc.list_connections(db)}
    # Every known provider is listed, so the UI can render "not connected"
    # without inventing entries.
    assert set(conns) == {"ollama", "openai", "anthropic", "huggingface"}
    assert conns["openai"]["api_key"] == "(set)"
    assert conns["openai"]["key_source"] == "env"
    assert conns["anthropic"]["api_key"] == "(unset)"
    assert conns["anthropic"]["configured"] is False
    # Ollama needs no credential, so it counts as configured.
    assert conns["ollama"]["requires_key"] is False
    assert conns["ollama"]["configured"] is True
    # The raw value must appear nowhere in the payload.
    assert "sk-from-env" not in repr(conns)


@pytest.mark.asyncio
async def test_stored_key_wins_over_env_and_is_reported_as_db():
    from pydantic import SecretStr
    from app.modules import provider_connections as pc
    from app.utils import secrets
    secrets.reset_cache()
    enc = secrets.encrypt("sk-from-db")
    db = _db([{ "provider": "openai", "api_key_enc": enc, "base_url": None,
                "enabled": True, "label": None, "last_ok_at": None,
                "last_error": None }])
    with patch("app.config.settings.openai_api_key", SecretStr("sk-from-env")):
        conns = {c["provider"]: c for c in await pc.list_connections(db)}
    assert conns["openai"]["key_source"] == "db"
    assert conns["openai"]["key_unreadable"] is False


@pytest.mark.asyncio
async def test_undecryptable_key_is_surfaced_not_swallowed():
    """A ciphertext we can no longer read must be visible in the UI — the
    operator has to know to re-enter it."""
    from app.modules import provider_connections as pc
    db = _db([{ "provider": "openai", "api_key_enc": "garbage-not-fernet",
                "base_url": None, "enabled": True, "label": None,
                "last_ok_at": None, "last_error": None }])
    conns = {c["provider"]: c for c in await pc.list_connections(db)}
    assert conns["openai"]["key_unreadable"] is True


@pytest.mark.asyncio
async def test_set_connection_rejects_unknown_provider():
    from app.modules import provider_connections as pc
    with pytest.raises(ValueError, match="unknown provider"):
        await pc.set_connection("cohere", api_key="x", db=_db())


@pytest.mark.asyncio
async def test_set_connection_rejects_a_key_for_a_keyless_provider():
    from app.modules import provider_connections as pc
    with pytest.raises(ValueError, match="takes no API key"):
        await pc.set_connection("ollama", api_key="nope", db=_db())


@pytest.mark.asyncio
async def test_load_into_settings_is_fail_soft_on_a_dead_table():
    """Startup must survive a pre-migration / unavailable table."""
    from app.modules import provider_connections as pc
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("relation does not exist"))
    assert await pc.load_connections_into_settings(db) == 0


@pytest.mark.asyncio
async def test_test_connection_classifies_a_bad_key():
    """A 401 must read as 'the key was rejected', not as generic
    unreachability — that is the distinction an operator needs."""
    import httpx
    from app.modules import provider_connections as pc
    req = httpx.Request("GET", "https://api.openai.com/v1/models")
    resp = httpx.Response(401, request=req, text="Unauthorized")
    err = httpx.HTTPStatusError("401", request=req, response=resp)
    with patch.object(pc, "list_provider_models", AsyncMock(side_effect=err)):
        out = await pc.test_connection("openai", db=None)
    assert out["ok"] is False
    assert "key was rejected" in out["detail"]


@pytest.mark.asyncio
async def test_test_connection_classifies_an_unreachable_endpoint():
    import httpx
    from app.modules import provider_connections as pc
    with patch.object(pc, "list_provider_models",
                      AsyncMock(side_effect=httpx.ConnectError("refused"))):
        out = await pc.test_connection("ollama", db=None)
    assert out["ok"] is False
    assert "cannot reach" in out["detail"]


@pytest.mark.asyncio
async def test_clearing_a_key_reverts_to_env_not_to_blank():
    """"Clear" and "Forget" mean revert to the ENVIRONMENT value.

    The subtle bug this pins: `Settings.model_fields[f].default` is config.py's
    hardcoded default, NOT the env-loaded value — reverting to it would blank an
    OPENAI_API_KEY that .env legitimately supplied, turning "undo my UI change"
    into "break the install's original config".
    """
    from pydantic import SecretStr
    from app.config import settings
    from app.modules import provider_connections as pc

    pc._ENV_BASELINE.clear()
    original = settings.openai_api_key
    try:
        settings.openai_api_key = SecretStr("sk-from-dot-env")
        pc.capture_env_baseline()                     # as lifespan startup does
        settings.openai_api_key = SecretStr("sk-set-in-the-ui")   # a stored key

        await pc.set_connection("openai", api_key="", db=_db())   # explicit clear
        assert settings.openai_api_key.get_secret_value() == "sk-from-dot-env"

        settings.openai_api_key = SecretStr("sk-set-again")
        await pc.delete_connection("openai", _db())               # forget
        assert settings.openai_api_key.get_secret_value() == "sk-from-dot-env"
    finally:
        settings.openai_api_key = original
        pc._ENV_BASELINE.clear()


def test_capture_env_baseline_is_idempotent():
    """It must not re-capture after a stored key has been mirrored on top."""
    from pydantic import SecretStr
    from app.config import settings
    from app.modules import provider_connections as pc
    pc._ENV_BASELINE.clear()
    original = settings.openai_api_key
    try:
        settings.openai_api_key = SecretStr("env-value")
        pc.capture_env_baseline()
        settings.openai_api_key = SecretStr("db-value")
        pc.capture_env_baseline()                     # must be a no-op
        assert pc._env_value("openai_api_key").get_secret_value() == "env-value"
    finally:
        settings.openai_api_key = original
        pc._ENV_BASELINE.clear()


def test_every_registered_provider_has_a_connection_spec():
    """A provider the registry serves but connections cannot configure would be
    invisible in the UI and unsettable — keep the two lists in lockstep."""
    from app.config import valid_provider_names
    from app.modules.provider_connections import known_providers
    assert set(known_providers()) == set(valid_provider_names())
