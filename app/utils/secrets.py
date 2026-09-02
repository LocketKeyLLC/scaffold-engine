"""§17.900 — symmetric encryption for provider credentials at rest.

Provider API keys (`provider_connections.api_key_enc`) are the first secrets
this engine stores in Postgres rather than reading from the environment. They
are encrypted with Fernet (AES-128-CBC + HMAC-SHA256, from `cryptography`) so a
`pg_dump`, a backup tarball, or a read-only DB grant does not hand out the
operator's OpenAI/Anthropic/HuggingFace keys.

The encryption key is derived, not stored:

    Fernet key = urlsafe_b64encode(HKDF-SHA256(secret, salt=b"scaffold-provider-conn-v1"))

where ``secret`` is ``SCAFFOLD_SECRET_KEY`` when set, else ``SCAFFOLD_API_KEY``
(always present — it is the engine's own auth key). Deriving from an existing
secret means plug-and-play works on a fresh install with nothing extra to
configure, while an operator who wants credential encryption decoupled from
API-key rotation can set ``SCAFFOLD_SECRET_KEY`` explicitly.

CONSEQUENCE, and it is deliberate: rotating the derivation secret makes stored
ciphertexts undecryptable. ``decrypt`` returns ``None`` on any failure rather
than raising, and callers fall back to the env value and surface a re-enter
prompt — a lost stored key must degrade to "reconnect this provider", never to
a 500 or a startup crash.
"""
from __future__ import annotations

import base64
import logging

logger = logging.getLogger("scaffold.secrets")

_SALT = b"scaffold-provider-conn-v1"
_INFO = b"provider-connection-api-key"

# Derivation is pure and hot-path-irrelevant (a handful of calls at startup),
# but caching keeps `decrypt` cheap if it is ever called per request.
_cached: tuple[str, object] | None = None


def _derivation_secret() -> str:
    """The raw secret the Fernet key is derived from. Never returned to callers."""
    from app.config import settings
    explicit = getattr(settings, "scaffold_secret_key", None)
    if explicit is not None:
        raw = explicit.get_secret_value() if hasattr(explicit, "get_secret_value") else str(explicit)
        if (raw or "").strip():
            return raw.strip()
    api_key = settings.scaffold_api_key
    raw = api_key.get_secret_value() if hasattr(api_key, "get_secret_value") else str(api_key)
    return (raw or "").strip()


def _fernet():
    """Build (and cache) the Fernet instance for the current derivation secret."""
    global _cached
    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF

    secret = _derivation_secret()
    if not secret:
        raise ValueError(
            "no derivation secret available (SCAFFOLD_SECRET_KEY and "
            "SCAFFOLD_API_KEY are both empty) — cannot encrypt provider keys"
        )
    if _cached is not None and _cached[0] == secret:
        return _cached[1]
    raw = HKDF(algorithm=hashes.SHA256(), length=32, salt=_SALT,
               info=_INFO).derive(secret.encode("utf-8"))
    f = Fernet(base64.urlsafe_b64encode(raw))
    _cached = (secret, f)
    return f


def encrypt(plaintext: str) -> str:
    """Encrypt a credential for storage. Raises if no derivation secret exists —
    a write that cannot be protected must fail loudly, not silently store
    plaintext."""
    if plaintext is None:
        raise ValueError("cannot encrypt None")
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt(ciphertext: str | None) -> str | None:
    """Decrypt a stored credential, or None if it cannot be read.

    Fail-soft by contract: a rotated derivation secret, a corrupted row, or a
    value written by a different install all return None so the caller falls
    back to the env value and prompts for re-entry. The failure is logged
    WITHOUT the ciphertext."""
    if not ciphertext:
        return None
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception as e:  # noqa: BLE001 — see docstring
        logger.warning(
            "provider_credential_undecryptable err=%s — falling back to env; "
            "re-enter the key in Settings → Connections to restore it",
            type(e).__name__,
        )
        return None


def mask(value: str | None) -> str:
    """The ONLY representation of a credential the API may return.

    Mirrors /config's redaction vocabulary (§17.611) so every surface says the
    same thing about a secret: it is set, or it is not. Never a prefix, never a
    length — both leak."""
    return "(set)" if (value or "").strip() else "(unset)"


def reset_cache() -> None:
    """Drop the cached Fernet (tests, and after a secret rotation)."""
    global _cached
    _cached = None
