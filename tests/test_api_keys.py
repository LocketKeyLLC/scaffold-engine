"""§17.807 — unit tests for app/modules/api_keys.py (pure helpers).

The DB-backed functions (add_key/list_keys/revoke_key/verify_key) are exercised
against a live Postgres in the integration suite; here we cover the pure logic:
deterministic hashing, key-format/entropy, and revoke_key's argument guard
(which raises before any query, so it needs no DB).
"""
import hashlib

import pytest

from app.modules import api_keys as ak


@pytest.mark.smoke
def test_hash_key_is_sha256_hex():
    raw = "sk-scaffold-abc123"
    assert ak.hash_key(raw) == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert len(ak.hash_key(raw)) == 64  # sha256 hex


@pytest.mark.smoke
def test_hash_key_deterministic_and_distinct():
    assert ak.hash_key("same") == ak.hash_key("same")
    assert ak.hash_key("a") != ak.hash_key("b")


@pytest.mark.smoke
def test_generate_raw_key_prefixed_and_unique():
    k1 = ak.generate_raw_key()
    k2 = ak.generate_raw_key()
    assert k1.startswith("sk-scaffold-")
    assert k2.startswith("sk-scaffold-")
    assert k1 != k2                       # token_urlsafe entropy
    assert len(k1) > len("sk-scaffold-") + 30


@pytest.mark.smoke
async def test_verify_key_empty_is_false_without_db():
    """An empty presented key short-circuits to False before any query."""
    assert await ak.verify_key(session=None, raw="") is False


@pytest.mark.smoke
async def test_resolve_key_empty_is_none_without_db():
    """§17.810 — resolve_key short-circuits an empty key to None before any query."""
    assert await ak.resolve_key(session=None, raw="") is None


@pytest.mark.smoke
async def test_add_key_rejects_bad_role_before_db():
    """§17.810 — an invalid role is rejected up front (session never touched)."""
    with pytest.raises(ValueError):
        await ak.add_key(session=None, label="x", role="superuser")


@pytest.mark.smoke
async def test_revoke_key_requires_exactly_one_selector():
    """revoke_key guards its args before touching the DB, so session=None is fine."""
    with pytest.raises(ValueError):
        await ak.revoke_key(session=None)  # neither id nor label
    with pytest.raises(ValueError):
        await ak.revoke_key(session=None, key_id=1, label="x")  # both
